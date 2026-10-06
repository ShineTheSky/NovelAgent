"""Await recall before model context assembly; only actual requests mature Insight."""

import re
import time
import logging

from novelagent.context.token_counter import TokenCounter
from novelagent.memory.models import chosen_solution, render_knowledge
from novelagent.memory.retrieval import KnowledgeRetrieval
from novelagent.history.store import DocumentHistoryStore


class KnowledgeRuntime:
    def __init__(self, pipeline, config=None):
        self.pipeline, self.config = pipeline, config or {}

    @staticmethod
    def paths(query):
        return list(dict.fromkeys(re.findall(r"(?:chapters|outlines|characters|world)/[\w.\-\u4e00-\u9fff]+\.md", query.replace("\\", "/"))))

    @staticmethod
    def content_type(paths):
        types = set()
        for path in paths:
            if path.startswith("chapters/"):
                types.add("chapter")
            elif re.fullmatch(r"outlines/outline_\d+\.0\.0\.md", path):
                types.add("volume_outline")
            elif path.startswith("outlines/"):
                types.add("chapter_outline")
            elif path.startswith("characters/"):
                types.add("character")
            elif path.startswith("world/"):
                types.add("world")
        return next(iter(types)) if len(types) == 1 else ""

    async def assemble(self, project_id, query, *, role="main", paths=(), content_type="", session_id="", trace_id=""):
        started = time.perf_counter()
        try:
            return await self._assemble(project_id, query, role=role, paths=paths, content_type=content_type,
                                        session_id=session_id, trace_id=trace_id)
        except Exception as exc:
            try:
                await self.pipeline.capture_failure(project_id, {"session_id": session_id, "trace_ids": [trace_id],
                    "history_id": ""}, exc, "assembly")
            except Exception:
                logging.exception("Failed to persist knowledge assembly bad case")
            return {"text": "", "attachments": [], "recall": {"status": "failed", "error": str(exc),
                "elapsed_ms": (time.perf_counter()-started)*1000, "routes": {}}}

    async def _assemble(self, project_id, query, *, role, paths, content_type, session_id, trace_id):
        started = time.perf_counter()
        store = self.pipeline.store(project_id)
        paths = paths or self.paths(query)
        content_type = content_type or self.content_type(paths)
        recall = await self.pipeline.retrieval(project_id).recall(query, purpose="task_insight", role=role, paths=paths,
                                                               content_type=content_type, limit=self.config.get("insight_limit", 12))
        if recall["status"] in {"failed", "timeout"}:
            await self.pipeline.capture_failure(project_id, {"session_id": session_id, "history_id": recall["audit_id"],
                                                            "trace_ids": [trace_id or recall["audit_id"]]}, recall.get("error", "Recall failed"), "recall")
        attachments, omitted = [], []
        tokens = TokenCounter()
        for layer, candidates, budget in (
            ("pattern", store.list(("pattern",)), self.config.get("pattern_tokens", 8000)),
            ("insight", [r["record"] for r in recall["items"]], self.config.get("insight_tokens", 8000)),
        ):
            used = 0
            for record in candidates:
                if not KnowledgeRetrieval.applicable(record, role, paths, content_type):
                    continue
                selected = chosen_solution(record["payload"], role, paths, content_type)
                if record["payload"].get("solutions") and selected is None:
                    omitted.append({"id": record["id"], "reason": "no_applicable_solution"})
                    continue
                sid = selected["solution_id"] if selected else ""
                marker = f"[knowledge:{record['id']}@{record['version']}/solution:{sid}]"
                view = {**record, "payload": {**record["payload"], "solutions": [selected] if selected else []}}
                text = f"{marker}\n### {layer.upper()}：{record['payload']['title']}\n{render_knowledge(view, role, paths, content_type)}"
                size = tokens.count(text)
                if used + size > budget:
                    omitted.append({"id": record["id"], "reason": "context_budget"})
                    continue
                used += size
                attachments.append({"id": record["id"], "version": record["version"], "layer": layer,
                                    "solution_id": sid, "marker": marker, "text": text})
        selected = [{k: a[k] for k in ("id", "version", "solution_id")} for a in attachments]
        audit_id = store.audit({"stage": "assembly", "role": role, "recall_audit_id": recall["audit_id"],
                               "selected": selected, "omitted": omitted})
        recall.update(assembled=selected, omitted=omitted, assembly_audit_id=audit_id,
                      assembly_elapsed_ms=(time.perf_counter()-started)*1000)
        header = ("【本阶段项目知识】只在所列条件和范围内使用；当前用户明确要求优先。"
                  "继承上下文中其他角色的知识是过程证据，不等于当前阶段约束。\n")
        return {"text": header+"\n\n".join(a["text"] for a in attachments) if attachments else "",
                "attachments": attachments, "recall": {k: v for k, v in recall.items() if k != "items"}}

    def index(self, project_id):
        try:
            rows = self.pipeline.store(project_id).list(("memory",))
        except Exception:
            logging.exception("Knowledge title window unavailable")
            return "# Memory 事实包标题窗口\n当前索引读取失败；不能据此认定没有项目记忆。"
        return "# Memory 事实包标题窗口\n\n" + "\n".join(
            f"- {r['id']}@{r['version']}：{r['payload']['title']}" for r in rows[:200]) + \
            f"\n\n显示 {min(len(rows), 200)} / {len(rows)}；窗口外仍可通过 SearchMemory 召回或按 ID 回读。"

    def mark_used(self, project_id, attachments, messages, task_id, role):
        contents = "\n".join(str(m.get("content") or "") for m in messages)
        store = self.pipeline.store(project_id)
        history = DocumentHistoryStore(store.project_dir)
        task_id = history.canonical_root(history._unit_id(task_id, 'request'))
        for attachment in attachments:
            if attachment["layer"] == "insight" and attachment["text"] in contents:
                try:
                    store.mark_used(attachment["id"], attachment["version"], task_id,
                                    attachment["solution_id"], role, self.config.get("pattern_after_uses", 0),
                                    score_threshold=self.config.get("pattern_promotion_weight", 200))
                except Exception:
                    logging.exception("Knowledge maturity write failed; model execution continues")
