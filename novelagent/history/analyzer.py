"""Derive durable memories from completed History units, not Trace windows."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from copy import deepcopy
from pathlib import Path

from novelagent.history.store import DocumentHistoryStore
from novelagent.llm.json_output import json_object
from novelagent.llm.failures import llm_failure_context
from novelagent.trace.file_lifecycle import FileLifecycleStore, PATTERN_PROMOTION_WEIGHT
from novelagent.trace.semantic_extraction import (
    build_semantic_extraction_section,
    normalize_analysis_semantics,
    select_extraction_profiles,
)


class HistoryAnalyzer:
    _VERSION_EVENTS = {"revision", "revision_reference", "initial_context"}

    @classmethod
    def _extraction_events(cls, unit: dict) -> list[dict]:
        split_review = any(e["kind"] in {"reviewer_requirement_report", "reviewer_general_report"} for e in unit["events"])
        return [e for e in unit["events"] if e["kind"] not in cls._VERSION_EVENTS
                and not (split_review and e["kind"] == "reviewer_output")]

    @staticmethod
    def _source_kind(events: list[dict]) -> str:
        kinds = {e["kind"] for e in events}
        return ("user" if kinds & {"user_input", "user_answer"} else
                "reviewer" if any(kind.startswith("reviewer_") for kind in kinds) else
                "agent" if any(kind.endswith("_thinking") or kind.endswith("_output") for kind in kinds) else "history")

    def __init__(self, llm_client, workspace_dir: str, trace_store, knowledge_pipeline=None):
        self.llm = llm_client
        self.workspace_dir = Path(workspace_dir)
        self.trace_store = trace_store
        self.knowledge_pipeline = knowledge_pipeline
        self._locks: dict[str, asyncio.Lock] = {}

    async def _chat_json(self, prompt: str, tag: str, context: list[dict] | None = None) -> dict:
        chunks = []
        async for chunk in self.llm.chat(
            position="memory_summary_fallback",
            messages=[*deepcopy(context or []), {"role": "user", "content": prompt}], tools=None,
            stream=False, tag=tag, json_mode=True,
        ):
            if chunk.type == "text_delta":
                chunks.append(chunk.content)
            elif chunk.type == "error":
                raise RuntimeError(chunk.error or "History analysis failed")
        raw = "".join(chunks).strip()
        return json_object(raw)

    async def _select_conversation_turns(self, unit: dict, store: DocumentHistoryStore) -> dict:
        turns = await self.trace_store.list_session_turns(unit["session_id"]) if self.trace_store else []
        candidates = [row for row in turns if int(row["turn_no"]) < int(unit["session_turn_no"] or 0)][-40:]
        if not candidates:
            selection = {"ranges": [], "reason": "本会话没有可用的既往 Session turn", "turns": []}
            store.set_selected_turns(unit["history_id"], selection)
            return selection
        compact = [{"turn_no": row["turn_no"], "user": row["user_content"][:1600],
                    "assistant": row["assistant_content"][:1600]} for row in candidates]
        decision = await self._chat_json(
            "从以下 Session turn 中选择理解本轮对话真正需要的先前回合。只用给定 turn_no，"
            "可选多个闭区间；不相关的不选。输出 JSON: "
            '{"ranges":[{"start":1,"end":2}],"reason":"选择依据"}。'
            f"\n本轮用户输入：{next((e['content'] for e in unit['events'] if e['kind'] == 'user_input'), '')}"
            f"\n候选回合：{json.dumps(compact, ensure_ascii=False)}",
            ":history/select-turns",
        )
        available = {int(row["turn_no"]): row for row in candidates}
        selected: set[int] = set()
        ranges = []
        for entry in decision.get("ranges", []):
            if not isinstance(entry, dict):
                continue
            try:
                start, end = int(entry["start"]), int(entry["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start > end or end - start > 40 or any(no not in available for no in range(start, end + 1)):
                continue
            ranges.append({"start": start, "end": end})
            selected.update(range(start, end + 1))
        selected_rows = [available[no] for no in sorted(selected)[-20:]]
        selection = {"ranges": ranges, "reason": str(decision.get("reason") or ""),
                     "turns": selected_rows}
        store.set_selected_turns(unit["history_id"], selection)
        return selection

    @staticmethod
    def _event_view(unit: dict) -> list[dict]:
        return [{"type": "user_message" if event["kind"] in {"user_input", "user_answer"} else event["kind"],
                 "content": event["content"],
                 "preset": "reviewer" if event["kind"].startswith("reviewer_") else "",
                 "params": {"path": unit.get("path", "")}}
                for event in HistoryAnalyzer._extraction_events(unit)]

    @staticmethod
    def _evidence_view(unit: dict) -> dict:
        revisions = []
        for row in unit.get("revisions", []):
            if not row:
                continue
            revisions.append({
                "revision_id": row["revision_id"], "parent_revision_id": row["parent_revision_id"],
                "version_no": row["version_no"], "path": row["path"],
                "body_excerpt": row["body_md"][:12000], "dependencies": row["dependencies"],
                "evidence": row["evidence"], "no_change": row["no_change"],
            })
        return {
            "history_id": unit["history_id"], "kind": unit["kind"], "path": unit["path"],
            "session_turn_no": unit["session_turn_no"],
            "base_revision_id": unit["base_revision_id"],
            "final_revision_id": unit["final_revision_id"],
            "previous_history_id": unit.get("previous_history_id", ""),
            "requirement_history_id": unit.get("requirement_history_id", ""),
            "requirement_event_ids": unit.get("requirement_event_ids", []),
            "previous_attempt_id": unit["previous_attempt_id"],
            "previous_attempt_events": [
                {"kind": e["kind"], "content": e["content"], "revision_id": e["revision_id"]}
                for e in unit.get("previous_attempt_events", [])
                if e["kind"] not in HistoryAnalyzer._VERSION_EVENTS
            ],
            "events": [{"event_id": e["event_id"], "kind": e["kind"],
                        "content": e["content"], "revision_id": e["revision_id"],
                        "event_order": e.get("event_order"), "occurred_at": e.get("occurred_at", ""),
                        "order_status": e.get("order_status", ""), "original_event_id": e.get("original_event_id", ""),
                        "evidence_history_id": e.get("evidence_history_id", unit["history_id"]),
                        "inherited_requirement": e.get("inherited_requirement", False)}
                        | {"inherited_review": e.get("inherited_review", False)}
                        | {"scope": e.get("scope", {})}
                       for e in HistoryAnalyzer._extraction_events(unit)],
            "revisions": revisions,
            "selected_turns": unit["selected_turns"],
            "actual_model_context": unit["context"][-12:],
            "initial_model_context": next((json.loads(e["content"]) for e in unit["events"] if e["kind"] == "initial_context"), []),
            "assistant_response": unit["assistant_response"],
        }

    async def analyze(self, project_id: str, history_id: str, *, force: bool = False) -> None:
        store = DocumentHistoryStore(self.workspace_dir / project_id)
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            history_id = store.canonical_root(history_id)
            unit = store.get_unit(history_id)
            partial = bool(unit and self.knowledge_pipeline and unit["status"] in {"failed", "interrupted"}
                           and unit["kind"] in {"requirement", "conversation"})
            if not unit or (unit["status"] != "completed" and not partial):
                return
            if not force and unit["analysis_status"] == "complete" and not (
                self.knowledge_pipeline and self.knowledge_pipeline.store(project_id).has_input(history_id)
            ):
                return
            workflow = unit.get("review_workflow") if unit["kind"] == "review" else {}
            if workflow and workflow.get("status") != "completed":
                return
            if unit["kind"] in {"document", "review"} and unit.get("requirement_history_id"):
                requirement = store.get_unit(unit["requirement_history_id"])
                if not workflow and (not requirement or requirement["status"] != "completed"):
                    return
            context_token = llm_failure_context.set({"project_id": project_id, "history_id": history_id,
                "session_id": unit["session_id"], "source_trace_id": (unit.get("trace_ids") or [""])[0]})
            try:
                if self.knowledge_pipeline and unit["kind"] in {"requirement", "conversation"}:
                    if not unit["selected_turns"]:
                        await self._select_conversation_turns(unit, store)
                        unit = store.get_unit(history_id)
                    unit = await self.knowledge_pipeline.resume_history(project_id, unit)
                    history_id = unit["history_id"]
                if self.knowledge_pipeline and unit["kind"] == "document" and unit.get("requirement_history_id"):
                    requirement = store.get_unit(unit["requirement_history_id"])
                    if requirement and requirement["kind"] == "requirement":
                        store.set_analysis_status(history_id, "routed")
                        return
                if workflow:
                    # Resolve provenance, not a second model-context reconstruction.
                    for result_id in workflow["result_history_ids"]:
                        result = store.get_unit(result_id)
                        if not result or result["status"] != "completed" or result["review_history_id"] != history_id:
                            raise ValueError("Completed polish History is unavailable")
                        unit["events"].extend(e for e in result["events"]
                                              if not e.get("inherited_requirement") and not e.get("inherited_review"))
                        known = {r["revision_id"] for r in unit["revisions"] if r}
                        unit["revisions"].extend(r for r in result["revisions"] if r and r["revision_id"] not in known)
                if unit["kind"] in {"conversation", "requirement"} and not unit["selected_turns"]:
                    await self._select_conversation_turns(unit, store)
                    unit = store.get_unit(history_id)
                if self.knowledge_pipeline:
                    if await self.knowledge_pipeline.process(project_id, unit, retry_failed=force) is False:
                        store.set_analysis_status(history_id, "deferred", "Memory direction requires clarification")
                        return
                    store.set_analysis_status(history_id, "complete")
                    return
                prepared = unit["prepared"]
                if not prepared:
                    profiles = select_extraction_profiles(self._event_view(unit))
                    files = FileLifecycleStore(str(self.workspace_dir), project_id)
                    existing = [{"id": item["id"], "layer": layer,
                                 "claim": item.get("claim", ""),
                                 "semantic_evidence": item.get("semantic_evidence", [])[-3:],
                                 "history_ids": item.get("history_ids", []),
                                 "evidence_history_ids": list(dict.fromkeys([
                                     *(row["history_id"] for row in item.get("semantic_evidence", [])
                                       if isinstance(row, dict) and row.get("history_id")),
                                     *(row["history_id"] for row in item.get("history_source_links", [])
                                       if row.get("match") == "evidence"),
                                 ])),
                                 "history_revision_ids": item.get("history_revision_ids", [])}
                                for layer in ("insight", "memory", "pattern")
                                for item in files.list(layer)[:80]]
                    if workflow:
                        evidence = {"history_id": history_id, "kind": "review", "path": unit["path"],
                                    "result_history_ids": workflow["result_history_ids"],
                                    "events": [{key: event.get(key, "") for key in
                                                ("event_id", "kind", "revision_id", "evidence_history_id", "scope")}
                                               for event in self._extraction_events(unit)]}
                    else:
                        evidence = self._evidence_view(unit)
                    response = await self._chat_json(
                        ("【服务端阶段切换：审阅记忆提取】\n写作、审阅和润色已经结束。"
                         "以上完整上下文是本次证据，只分析、不再审阅或修改文件，不得调用工具。"
                         "下面 History 仅提供真实证据 ID 和归属索引，不重复拼装正文、思考或报告。"
                         "区分审阅提出的问题与润色实际落实的结果；没有实际工具写入或明确无需修改的证据，"
                         "不能把最终回复中的完成宣称当作已落实。\n" if workflow else "")
                        + "[History 记忆提取]\nHistory 是证据根源；Trace 仅用于诊断和上下文摘要。"
                        "只保留未来值得复用的事实；无需记忆时 records=[]。不得把模型推测当用户意见，"
                        "不得把审阅建议当已确认的用户偏好。修订 ID 仅用于定位版本，不能作为记忆事实或引用证据。"
                        "针对文件的修改，比较本单元全部版本和评价，"
                        "需求 History 只提取用户要求、设定、偏好与方向变化，不推断文件已落实。"
                        "文件 History 只提取实际落实、理解差异和审阅修正经验；继承的需求仅作上下文，"
                        "不得单凭继承需求重复提炼用户偏好。文件候选必须引用至少一条本文件的执行或评价证据。"
                        "审阅 History 是需求下独立的特殊证据节点，不等于用户的新需求。"
                        "reviewer_requirement_report 只反映明确需求的验收，reviewer_general_report 反映其他常规写作问题；"
                        "必须区分两者，不把常规问题归因于本轮用户要求。审阅建议未经确认不能转成用户偏好。"
                        "审阅候选必须引用本节点的审阅报告，不得仅凭继承需求或思考提取；记忆可直接关联审阅 History。"
                        "文件中的继承审阅仅作上下文，不得仅凭复制的审阅报告重复增加支持次数。"
                        "分析用户需求、Agent 思考与实际改法之间的差异。"
                        "离线修复的事件按 event_order 排列；order_status=unverified 不可作为先后因果依据。"
                        "scope.evidence_scope=run 表示共享的运行上下文，不能认定每段思考都专门针对当前文件。"
                        "每条记录必须引用下方真实 event_id（source_event_ids），并给出与现有记录的关系。"
                        "若现有语义证据不足以判断合并关系，把对应 history_id 或 revision_id 放入 needs_history_ids，"
                        "history_ids 是根节点入口，evidence_history_ids 是精确证据所在的文件或对话节点，可按需回读。"
                        "不要凭相似措辞硬合并。输出 JSON: {\"needs_history_ids\":[],\"records\":[{\"layer\":\"insight|memory\",\"signal\":\"weak|strong\","
                        "\"title\":\"\",\"claim\":\"\",\"content\":\"\",\"category\":\"user|project|reference|agent\","
                        "\"domain\":\"writing|outline|overall\",\"kind\":\"\",\"source_event_ids\":[\"\"],"
                        "\"relation\":\"new|support|append|conflict\",\"related_layer\":\"\",\"related_id\":\"\",\"relation_reason\":\"\","
                        "\"source_text\":\"用户给出的原文原样保留，无则空\",\"semantic\":{}}]}。"
                        "需求型文本反馈可给出 artifact_path，但必须是提供的文档引用或关联文件中的真实路径。"
                        f"\n{build_semantic_extraction_section(profiles)}"
                        f"\nHistory:{json.dumps(evidence, ensure_ascii=False)}"
                        f"\nExisting:{json.dumps(existing, ensure_ascii=False)}",
                        ":history/extract",
                        **({"context": workflow["context"]} if workflow else {}),
                    )
                    requested_ids = [str(value) for value in response.get("needs_history_ids", [])]
                    allowed_ids = {str(value) for row in existing
                                   for key in ("history_ids", "evidence_history_ids", "history_revision_ids")
                                   for value in row.get(key, [])}
                    originals = {}
                    for requested_id in requested_ids[:6]:
                        if requested_id not in allowed_ids:
                            continue
                        original = store.get_unit(requested_id) or store.get_revision(requested_id)
                        if original:
                            originals[requested_id] = original
                    if originals and response.get("records"):
                        decisions = await self._chat_json(
                            "仅根据按需回读的原始 History，复核候选与现有记录的关系；"
                            "不增删或改写候选事实。输出 JSON: "
                            '{"decisions":[{"candidate_index":0,"relation":"new|support|append|conflict",'
                            '"related_layer":"insight|memory|pattern","related_id":""}]}。'
                            f"\nCandidates:{json.dumps(response['records'], ensure_ascii=False)}"
                            f"\nExisting:{json.dumps(existing, ensure_ascii=False)}"
                            f"\nOriginal History:{json.dumps(originals, ensure_ascii=False, default=str)}",
                            ":history/reconcile",
                            **({"context": workflow["context"]} if workflow else {}),
                        )
                        for decision in decisions.get("decisions", []):
                            try:
                                candidate = response["records"][int(decision["candidate_index"])]
                            except (KeyError, IndexError, TypeError, ValueError):
                                continue
                            if decision.get("relation") in {"new", "support", "append", "conflict"}:
                                for key in ("relation", "related_layer", "related_id"):
                                    candidate[key] = str(decision.get(key) or "")
                    prepared = normalize_analysis_semantics(response, profiles)
                    store.set_prepared(history_id, prepared)
                self._apply(project_id, unit, prepared)
                store.set_analysis_status(history_id, "complete")
            except Exception as exc:
                store.set_analysis_status(history_id, "failed", str(exc))
                raise
            finally:
                llm_failure_context.reset(context_token)

    def _apply(self, project_id: str, unit: dict, prepared: dict) -> None:
        files = FileLifecycleStore(str(self.workspace_dir), project_id)
        valid = {event["event_id"] for event in self._extraction_events(unit)}
        for index, raw in enumerate(prepared.get("records", [])):
            if not isinstance(raw, dict):
                continue
            ids = list(dict.fromkeys(str(value) for value in raw.get("source_event_ids", []) if str(value) in valid))
            claim = str(raw.get("claim") or "").strip()
            layer = str(raw.get("layer") or "")
            if not ids or not claim or layer not in {"insight", "memory"}:
                continue
            source_text = str(raw.get("source_text") or "")
            user_sources = [e["content"] for e in unit["events"]
                            if e["kind"] in {"user_input", "user_answer"}]
            selected_turns = unit.get("selected_turns")
            if isinstance(selected_turns, dict):
                user_sources.extend(str(row.get("user_content") or "")
                                    for row in selected_turns.get("turns", []))
            if source_text and not any(source_text in content for content in user_sources):
                source_text = ""
            references = [json.loads(e["content"]) for e in unit["events"] if e["kind"] == "document_reference"]
            known_paths = {str(row.get("path") or "") for row in references}
            known_paths.update(row["path"] for row in unit.get("affected_files", []))
            artifact_path = unit["path"] or (str(raw.get("artifact_path") or "") if raw.get("artifact_path") in known_paths else "")
            artifact_revision = unit["final_revision_id"] or unit["base_revision_id"]
            if not artifact_revision and artifact_path:
                artifact_revision = next((str(row.get("revision_id") or "") for row in references
                                          if row.get("path") == artifact_path and source_text and source_text in str(row.get("content") or "")), "")
            selected_events = [e for e in unit["events"] if e["event_id"] in ids]
            if unit["kind"] == "document" and selected_events and all(e.get("inherited_requirement") or e.get("inherited_review") for e in selected_events):
                continue
            if unit["kind"] == "document" and unit.get("review_history_id") and not any(
                not e.get("inherited_requirement") and not e.get("inherited_review")
                and e["kind"] not in {"main_delegation", "reviewer_delegation", "user_input", "user_answer", "question_ask", "document_reference"}
                for e in selected_events
            ):
                continue
            if unit["kind"] == "review" and not any(e["kind"] in {"reviewer_requirement_report", "reviewer_general_report", "reviewer_output"}
                                                      and not e.get("inherited_requirement") for e in selected_events):
                continue
            history_ids = list(dict.fromkeys(e.get("evidence_history_id", unit["history_id"]) for e in selected_events))
            evidence_owners = history_ids
            if unit["kind"] == "review":
                history_ids = [unit["history_id"]]
            source_keys = []
            for event in selected_events:
                support_id = unit["history_id"]
                if unit["kind"] == "review":
                    support_id = unit["history_id"]
                elif event.get("inherited_requirement"):
                    support_id = event["evidence_history_id"]
                elif event.get("inherited_review"):
                    support_id = event["evidence_history_id"]
                elif unit["kind"] == "document" and not event["kind"].startswith("reviewer_"):
                    support_id = unit.get("requirement_history_id") or support_id
                source_keys.append(f"history:{support_id}")
            source_keys = list(dict.fromkeys(source_keys))
            semantic = raw.get("semantic") if isinstance(raw.get("semantic"), dict) else {}
            revision_ids = [r["revision_id"] for r in unit.get("revisions", []) if r]
            evidence = [{"claim": semantic.get("what") or claim,
                         "history_id": owner, "history_event_ids": [e["event_id"] for e in selected_events if e.get("evidence_history_id", unit["history_id"]) == owner],
                         "extraction_history_id": unit["history_id"],
                         "record_index": index,
                         "history_revision_ids": revision_ids,
                         "source_kind": self._source_kind([e for e in selected_events if e.get("evidence_history_id", unit["history_id"]) == owner]),
                         "review_sections": list(dict.fromkeys(e.get("scope", {}).get("review_section") for e in selected_events
                             if e.get("evidence_history_id", unit["history_id"]) == owner and e.get("scope", {}).get("review_section"))),
                         "scope": artifact_path or "project",
                         "why": semantic.get("why", ""),
                         "attributes": semantic.get("attributes", {}),
                         "excerpt": source_text[:700],
                         "history_excerpts": [
                             {"event_id": e["event_id"], "kind": e["kind"],
                              "content": e["content"][:700]}
                             for e in selected_events if e.get("evidence_history_id", unit["history_id"]) == owner
                         ]} for owner in evidence_owners]
            related_layer = str(raw.get("related_layer") or "")
            related_id = str(raw.get("related_id") or "")
            related = files.get(related_layer, related_id) if related_layer in {"insight", "memory", "pattern"} and related_id else None
            if raw.get("kind") == "text_feedback" and source_text:
                matched_layer, matched = files.find_text_feedback_by_source_text(source_text)
                if matched and raw.get("relation") != "conflict":
                    related_layer, related = matched_layer, matched
                    raw = {**raw, "relation": "append"}
            if related and raw.get("relation") in {"support", "append"}:
                if any(row.get("extraction_history_id", row.get("history_id")) == unit["history_id"] and row.get("record_index") == index
                       for row in related.get("semantic_evidence", []) if isinstance(row, dict)):
                    continue
                fresh_support = any(key not in related.get("support_sources", []) for key in source_keys)
                related["support_sources"] = list(dict.fromkeys([*related.get("support_sources", []), *source_keys]))
                if raw.get("relation") == "support" and fresh_support:
                    related["support_count"] = int(related.get("support_count", 1)) + 1
                    related["weight"] = min(300, float(related.get("weight", 0)) +
                                            (35 if raw.get("signal") == "strong" else 15))
                related["history_ids"] = list(dict.fromkeys([*related.get("history_ids", []), *history_ids]))
                related["history_event_ids"] = list(dict.fromkeys([*related.get("history_event_ids", []), *ids]))
                related["history_revision_ids"] = list(dict.fromkeys([*related.get("history_revision_ids", []), *revision_ids]))
                related["semantic_evidence"] = [*related.get("semantic_evidence", []), *evidence]
                if source_text:
                    texts = list(related.get("source_texts") or [])
                    if not any(row.get("text") == source_text and row.get("history_id") == unit["history_id"]
                               for row in texts if isinstance(row, dict)):
                        texts.append({"text": source_text, "history_id": unit["history_id"],
                                      "artifact_path": artifact_path})
                    related["source_texts"] = texts
                saved = files.write(related_layer, related)
                if layer == "memory" and related_layer == "insight" and files.can_auto_promote(saved):
                    saved = files._move(saved, "memory")
                if saved["layer"] == "memory" and files.can_auto_promote(saved) and saved["weight"] >= PATTERN_PROMOTION_WEIGHT and saved["support_count"] >= 3:
                    files.promote_memory_to_pattern(saved, [])
                continue
            conflict = related and raw.get("relation") == "conflict" and related_layer in {"memory", "pattern"}
            if conflict:
                layer = "insight"
            stable_key = f"{unit['history_id']}:{index}"
            record_id = f"{layer[:3]}_{uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex}"
            if conflict:
                files.downgrade_conflict(related_layer, related_id, record_id,
                                         str(raw.get("relation_reason") or "同一适用范围内存在冲突"))
            existing = files.get(layer, record_id)
            if existing and all(key in existing.get("support_sources", []) for key in source_keys):
                continue
            record = {
                "id": record_id, "title": raw.get("title") or claim, "claim": claim,
                "content": raw.get("content") or claim,
                "category": raw.get("category") or "project", "domain": raw.get("domain") or "overall",
                "kind": raw.get("kind") or "", "confidence": raw.get("confidence", 0.7),
                "weight": 65 if raw.get("signal") == "strong" else 15, "support_count": 1,
                "support_sources": source_keys, "history_ids": history_ids,
                "history_revision_ids": revision_ids, "history_event_ids": ids,
                "semantic": semantic, "semantic_evidence": evidence,
                "artifact_path": artifact_path,
                "artifact_revision_id": artifact_revision,
            }
            if conflict:
                record.update({"promotion_status": "conflict_review",
                               "conflict_ids": [related_id],
                               "conflict_reason": str(raw.get("relation_reason") or "同一适用范围内存在冲突")})
            if source_text:
                record["source_text"] = source_text
                record["source_texts"] = [{"text": record["source_text"],
                                           "history_id": unit["history_id"],
                                           "artifact_path": artifact_path}]
            files.write(layer, record)
        files.rebuild_indexes()

    def interrupt_collecting(self) -> int:
        """Local crash recovery does not authorize any model request."""
        return sum(DocumentHistoryStore(path.parent.parent).interrupt_collecting()
                   for path in self.workspace_dir.glob("*/.history/history.sqlite3"))

    async def recover(self) -> int:
        count = 0
        if not self.workspace_dir.exists():
            return count
        for project_dir in self.workspace_dir.iterdir():
            if not project_dir.is_dir():
                continue
            store = DocumentHistoryStore(project_dir)
            store.interrupt_collecting()
            for history_id in store.list_analyzable(include_partial=bool(self.knowledge_pipeline)):
                unit = store.get_unit(history_id)
                if (self.knowledge_pipeline
                    and not self.knowledge_pipeline.store(project_dir.name).has_input(history_id)):
                    # Legacy History needs explicit opt-in, not paid bulk re-extraction on startup.
                    continue
                try:
                    await self.analyze(project_dir.name, history_id)
                except Exception as exc:
                    print(f"[history] recovery failed: {history_id}: {exc}", flush=True)
                count += 1
        if self.knowledge_pipeline:
            await self.knowledge_pipeline.recover()
            from novelagent.memory.migration import KnowledgeMigration
            for project in self.workspace_dir.iterdir():
                if project.is_dir() and (project / '.knowledge/knowledge.sqlite3').exists():
                    await KnowledgeMigration(self).recover(project.name)
        return count
