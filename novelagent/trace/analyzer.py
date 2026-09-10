"""LLM-driven conversion of traces into atomic memories and cross-memory patterns."""

import json
import re
from pathlib import Path
import yaml

from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager
from novelagent.trace.store import TraceStore


class PostTurnAnalyzer:
    """LLM understands the interaction; program persists and validates all relationships."""

    def __init__(self, llm_client, working_dir: str, store: TraceStore | None = None):
        self.llm = llm_client
        self.working_dir = working_dir
        self.store = store or TraceStore()

    async def analyze(self, trace_id: str, project_id: str) -> None:
        try:
            events = await self.store.list_events(trace_id)
            patterns = await self.store.list_patterns(project_id)
            result = await self._classify(events, patterns)
            valid_event_ids = {event["event_id"] for event in events}
            for item in result.get("memories", []):
                await self._persist_memory(trace_id, project_id, item, patterns, valid_event_ids)
        except Exception as exc:
            # Analysis must never affect the user-facing request path.
            print(f"[trace_analyzer] trace={trace_id} failed: {exc}", flush=True)

    async def _classify(self, events: list[dict], patterns: list[dict]) -> dict:
        relevant = []
        for event in events:
            if event["event_type"] not in {"user_message", "user_answer", "assistant_turn", "tool_result", "error"}:
                continue
            payload = event.get("payload", {})
            relevant.append({
                "event_id": event["event_id"], "type": event["event_type"], "actor": event["actor"],
                "content": str(payload.get("content") or payload.get("data") or payload.get("message") or "")[:1600],
            })
        candidates = [{
            "pattern_id": p["pattern_id"], "kind": p["kind"], "subtype": p["subtype"],
            "dimension": p["dimension"], "claim": p["canonical_claim"], "scope": p["scope"],
            "status": p["status"],
        } for p in patterns]
        prompt = f"""你是 NovelAgent 的交互记忆分析器。根据 Trace 仅提取可长期复用的原子记忆。

将内容分为：project_fact（项目事实/已确认创作决策）、preference（用户稳定要求）、issue（用户指出的错误或反复执行问题）。
issue 的 subtype 只能是 logic_error、context_error、execution_error、interpretation_error、quality_issue、attempt_error、system_error。
preference 的 subtype 可使用 writing_style、dialogue、pacing、plot、character、workflow、constraints。
不要把普通闲聊、一次性任务或模型自己的推测当记忆。每条必须引用输入中的 event_id。

对 preference 或 issue：在候选 Pattern 中选择完全匹配的一项，action=attach 或 contradict；没有匹配才 action=create，cluster_id 只能使用候选中的 pattern_id。
仅输出 JSON，不要 Markdown：
{{"memories":[{{"kind":"preference|issue|project_fact","subtype":"...","dimension":"...","claim":"规范化的简短陈述","scope":"global|project|arc|chapter|scene","confidence":0.0,"source_event_ids":["evt"],"action":"attach|contradict|create","cluster_id":"候选 ID 或空字符串","memory_type":"user|feedback|project|reference","summary":"一行摘要","content":"可选的详细记忆"}}]}}

Trace events:\n{json.dumps(relevant, ensure_ascii=False)}

Candidate patterns:\n{json.dumps(candidates, ensure_ascii=False)}"""
        text = ""
        async for chunk in self.llm.chat(position="auto_memory", messages=[{"role": "user", "content": prompt}], tools=None, stream=False, tag=":trace-analyzer"):
            if chunk.type == "text_delta":
                text += chunk.content
        data = self._parse_json(text)
        return data if isinstance(data.get("memories"), list) else {"memories": []}

    def _parse_json(self, text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end < start:
            return {"memories": []}
        data = json.loads(clean[start:end + 1])
        return data if isinstance(data, dict) else {}

    async def _persist_memory(self, trace_id: str, project_id: str, item: dict, known_patterns: list[dict],
                              valid_event_ids: set[str]) -> None:
        kind = str(item.get("kind", ""))
        if kind not in {"project_fact", "preference", "issue"}:
            return
        claim = str(item.get("claim", "")).strip()[:1000]
        if not claim:
            return
        source_event_ids = [str(value) for value in item.get("source_event_ids", []) if str(value) in valid_event_ids]
        if not source_event_ids:
            return
        subtype = str(item.get("subtype", ""))[:100]
        scope = str(item.get("scope", "project"))
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        memory_id = await self.store.create_memory(project_id, trace_id, source_event_ids, kind, subtype, claim, scope, confidence)

        memory_type = str(item.get("memory_type", ""))
        if memory_type not in {"user", "feedback", "project", "reference"}:
            memory_type = {"preference": "user", "issue": "feedback", "project_fact": "project"}[kind]
        path = await self._write_memory_file(project_id, memory_id, memory_type, item, trace_id, source_event_ids, claim)
        await self.store.set_memory_file_path(memory_id, path)

        if kind == "project_fact":
            return
        action = str(item.get("action", "create"))
        requested_id = str(item.get("cluster_id", ""))
        valid_ids = {pattern["pattern_id"] for pattern in known_patterns}
        if action in {"attach", "contradict"} and requested_id in valid_ids:
            pattern_id = requested_id
        else:
            pattern_id = await self.store.create_pattern(
                project_id, kind, subtype, str(item.get("dimension", ""))[:100], claim, scope, confidence
            )
        await self.store.attach_memory(pattern_id, memory_id, "contradict" if action == "contradict" else "support")
        await self._review_if_ready(pattern_id)

    async def _write_memory_file(self, project_id: str, memory_id: str, memory_type: str, item: dict,
                                 trace_id: str, source_event_ids: list[str], claim: str) -> str:
        project_dir = Path(self.working_dir) / project_id
        store = FileStore(str(project_dir))
        relative = f"{memory_type}/{memory_id}.md"
        frontmatter = {
            "memory_id": memory_id,
            "type": memory_type,
            "tags": [str(item.get("kind", "")), str(item.get("subtype", "")), str(item.get("dimension", ""))],
            "summary": str(item.get("summary") or claim)[:240],
            "scope": str(item.get("scope", "project")),
            "confidence": float(item.get("confidence", 0.5)),
            "source_trace_ids": [trace_id],
            "source_event_ids": source_event_ids,
        }
        body = str(item.get("content") or claim)
        store.write(relative, "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n\n" + body)
        IndexManager(store).rebuild()
        return f".memory/{relative}"

    async def _review_if_ready(self, pattern_id: str) -> None:
        pattern = await self.store.get_pattern(pattern_id)
        if not pattern or (pattern["support_count"] < 3 and pattern["contradiction_count"] == 0):
            return
        memories = await self.store.list_pattern_memories(pattern_id)
        evidence = [{"relation": item["relation"], "claim": item["claim"], "scope": item["scope"], "confidence": item["confidence"]} for item in memories]
        prompt = f"""审核以下跨 Trace 的记忆模式。仅输出 JSON：{{"status":"confirmed|tentative|disputed","confidence":0.0}}。
confirmed 仅用于至少三条独立支持且不存在实质冲突的稳定规律；冲突时为 disputed；否则 tentative。
Pattern: {json.dumps(pattern, ensure_ascii=False)}\nEvidence: {json.dumps(evidence, ensure_ascii=False)}"""
        text = ""
        async for chunk in self.llm.chat(position="auto_memory", messages=[{"role": "user", "content": prompt}], tools=None, stream=False, tag=":trace-review"):
            if chunk.type == "text_delta":
                text += chunk.content
        try:
            result = self._parse_json(text)
            status = result.get("status", "tentative")
            confidence = float(result.get("confidence", pattern["confidence"]))
        except Exception:
            return
        if status in {"confirmed", "tentative", "disputed"}:
            await self.store.set_pattern_review(pattern_id, status, max(0.0, min(1.0, confidence)))


class PreferenceContextProvider:
    def __init__(self, store: TraceStore | None = None):
        self.store = store or TraceStore()

    async def render(self, project_id: str, limit: int = 8) -> str:
        patterns = await self.store.list_patterns(project_id, limit=limit, statuses=("confirmed",))
        if not patterns:
            return ""
        return "\n".join(f"- {pattern['canonical_claim']}" for pattern in patterns)
