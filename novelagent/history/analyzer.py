"""Derive durable memories from completed History units, not Trace windows."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

from novelagent.history.store import DocumentHistoryStore
from novelagent.trace.file_lifecycle import FileLifecycleStore, PATTERN_PROMOTION_WEIGHT
from novelagent.trace.semantic_extraction import (
    build_semantic_extraction_section,
    normalize_analysis_semantics,
    select_extraction_profiles,
)


class HistoryAnalyzer:
    _VERSION_EVENTS = {"revision", "revision_reference"}

    def __init__(self, llm_client, workspace_dir: str, trace_store):
        self.llm = llm_client
        self.workspace_dir = Path(workspace_dir)
        self.trace_store = trace_store
        self._locks: dict[str, asyncio.Lock] = {}

    async def _chat_json(self, prompt: str, tag: str) -> dict:
        chunks = []
        async for chunk in self.llm.chat(
            position="memory_summary_fallback",
            messages=[{"role": "user", "content": prompt}], tools=None,
            stream=False, tag=tag,
        ):
            if chunk.type == "text_delta":
                chunks.append(chunk.content)
            elif chunk.type == "error":
                raise RuntimeError(chunk.error or "History analysis failed")
        raw = "".join(chunks).strip()
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise ValueError("History analyzer did not return JSON")
        result = json.loads(match.group())
        if not isinstance(result, dict):
            raise ValueError("History analyzer returned non-object JSON")
        return result

    async def _select_conversation_turns(self, unit: dict, store: DocumentHistoryStore) -> dict:
        turns = await self.trace_store.list_session_turns(unit["session_id"])
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
                for event in unit["events"] if event["kind"] not in HistoryAnalyzer._VERSION_EVENTS]

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
            "previous_attempt_id": unit["previous_attempt_id"],
            "previous_attempt_events": [
                {"kind": e["kind"], "content": e["content"], "revision_id": e["revision_id"]}
                for e in unit.get("previous_attempt_events", [])
                if e["kind"] not in HistoryAnalyzer._VERSION_EVENTS
            ],
            "events": [{"event_id": e["event_id"], "kind": e["kind"],
                        "content": e["content"], "revision_id": e["revision_id"]}
                       for e in unit["events"] if e["kind"] not in HistoryAnalyzer._VERSION_EVENTS],
            "revisions": revisions,
            "selected_turns": unit["selected_turns"],
            "actual_model_context": unit["context"][-12:],
            "assistant_response": unit["assistant_response"],
        }

    async def analyze(self, project_id: str, history_id: str) -> None:
        store = DocumentHistoryStore(self.workspace_dir / project_id)
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            unit = store.get_unit(history_id)
            if not unit or unit["status"] != "completed" or unit["analysis_status"] == "complete":
                return
            try:
                if unit["kind"] == "conversation" and not unit["selected_turns"]:
                    await self._select_conversation_turns(unit, store)
                    unit = store.get_unit(history_id)
                prepared = unit["prepared"]
                if not prepared:
                    profiles = select_extraction_profiles(self._event_view(unit))
                    files = FileLifecycleStore(str(self.workspace_dir), project_id)
                    existing = [{"id": item["id"], "layer": layer,
                                 "claim": item.get("claim", ""),
                                 "semantic_evidence": item.get("semantic_evidence", [])[-3:],
                                 "history_ids": item.get("history_ids", []),
                                 "history_revision_ids": item.get("history_revision_ids", [])}
                                for layer in ("insight", "memory", "pattern")
                                for item in files.list(layer)[:80]]
                    evidence = self._evidence_view(unit)
                    response = await self._chat_json(
                        "[History 记忆提取]\nHistory 是证据根源；Trace 仅用于诊断和上下文摘要。"
                        "只保留未来值得复用的事实；无需记忆时 records=[]。不得把模型推测当用户意见，"
                        "不得把审阅建议当已确认的用户偏好。修订 ID 仅用于定位版本，不能作为记忆事实或引用证据。"
                        "针对文件的修改，比较本单元全部版本和评价，"
                        "分析用户需求、Agent 思考与实际改法之间的差异。"
                        "每条记录必须引用下方真实 event_id（source_event_ids），并给出与现有记录的关系。"
                        "若现有语义证据不足以判断合并关系，把对应 history_id 或 revision_id 放入 needs_history_ids，"
                        "不要凭相似措辞硬合并。输出 JSON: {\"needs_history_ids\":[],\"records\":[{\"layer\":\"insight|memory\",\"signal\":\"weak|strong\","
                        "\"title\":\"\",\"claim\":\"\",\"content\":\"\",\"category\":\"user|project|reference|agent\","
                        "\"domain\":\"writing|outline|overall\",\"kind\":\"\",\"source_event_ids\":[\"\"],"
                        "\"relation\":\"new|support|append|conflict\",\"related_layer\":\"\",\"related_id\":\"\",\"relation_reason\":\"\","
                        "\"source_text\":\"用户给出的原文原样保留，无则空\",\"semantic\":{}}]}。"
                        f"\n{build_semantic_extraction_section(profiles)}"
                        f"\nHistory:{json.dumps(evidence, ensure_ascii=False)}"
                        f"\nExisting:{json.dumps(existing, ensure_ascii=False)}",
                        ":history/extract",
                    )
                    requested_ids = [str(value) for value in response.get("needs_history_ids", [])]
                    allowed_ids = {str(value) for row in existing
                                   for key in ("history_ids", "history_revision_ids")
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

    def _apply(self, project_id: str, unit: dict, prepared: dict) -> None:
        files = FileLifecycleStore(str(self.workspace_dir), project_id)
        valid = {event["event_id"] for event in unit["events"]
                 if event["kind"] not in self._VERSION_EVENTS}
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
            source_key = f"history:{unit['history_id']}"
            semantic = raw.get("semantic") if isinstance(raw.get("semantic"), dict) else {}
            revision_ids = [r["revision_id"] for r in unit.get("revisions", []) if r]
            selected_kinds = {e["kind"] for e in unit["events"] if e["event_id"] in ids}
            source_kind = ("user" if selected_kinds & {"user_input", "user_answer"} else
                           "reviewer" if any(kind.startswith("reviewer_") for kind in selected_kinds) else
                           "agent" if any(kind.endswith("_thinking") or kind.endswith("_output")
                                          for kind in selected_kinds) else "history")
            evidence = [{"claim": semantic.get("what") or claim,
                         "history_id": unit["history_id"], "history_event_ids": ids,
                         "record_index": index,
                         "history_revision_ids": revision_ids,
                         "source_kind": source_kind, "scope": unit["path"] or "project",
                         "why": semantic.get("why", ""),
                         "attributes": semantic.get("attributes", {}),
                         "excerpt": source_text[:700],
                         "history_excerpts": [
                             {"event_id": e["event_id"], "kind": e["kind"],
                              "content": e["content"][:700]}
                             for e in unit["events"] if e["event_id"] in ids
                         ]}]
            related_layer = str(raw.get("related_layer") or "")
            related_id = str(raw.get("related_id") or "")
            related = files.get(related_layer, related_id) if related_layer in {"insight", "memory", "pattern"} and related_id else None
            if raw.get("kind") == "text_feedback" and source_text:
                matched_layer, matched = files.find_text_feedback_by_source_text(source_text)
                if matched and raw.get("relation") != "conflict":
                    related_layer, related = matched_layer, matched
                    raw = {**raw, "relation": "append"}
            if related and raw.get("relation") in {"support", "append"}:
                if any(row.get("history_id") == unit["history_id"] and row.get("record_index") == index
                       for row in related.get("semantic_evidence", []) if isinstance(row, dict)):
                    continue
                fresh_support = source_key not in related.get("support_sources", [])
                related["support_sources"] = list(dict.fromkeys([*related.get("support_sources", []), source_key]))
                if raw.get("relation") == "support" and fresh_support:
                    related["support_count"] = int(related.get("support_count", 1)) + 1
                    related["weight"] = min(300, float(related.get("weight", 0)) +
                                            (35 if raw.get("signal") == "strong" else 15))
                related["history_ids"] = list(dict.fromkeys([*related.get("history_ids", []), unit["history_id"]]))
                related["history_revision_ids"] = list(dict.fromkeys([*related.get("history_revision_ids", []), *revision_ids]))
                related["semantic_evidence"] = [*related.get("semantic_evidence", []), *evidence]
                if source_text:
                    texts = list(related.get("source_texts") or [])
                    if not any(row.get("text") == source_text and row.get("history_id") == unit["history_id"]
                               for row in texts if isinstance(row, dict)):
                        texts.append({"text": source_text, "history_id": unit["history_id"],
                                      "artifact_path": unit["path"]})
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
            if existing and source_key in existing.get("support_sources", []):
                continue
            record = {
                "id": record_id, "title": raw.get("title") or claim, "claim": claim,
                "content": raw.get("content") or claim,
                "category": raw.get("category") or "project", "domain": raw.get("domain") or "overall",
                "kind": raw.get("kind") or "", "confidence": raw.get("confidence", 0.7),
                "weight": 65 if raw.get("signal") == "strong" else 15, "support_count": 1,
                "support_sources": [source_key], "history_ids": [unit["history_id"]],
                "history_revision_ids": revision_ids, "history_event_ids": ids,
                "semantic": semantic, "semantic_evidence": evidence,
                "artifact_path": unit["path"],
                "artifact_revision_id": unit["final_revision_id"] or unit["base_revision_id"],
            }
            if conflict:
                record.update({"promotion_status": "conflict_review",
                               "conflict_ids": [related_id],
                               "conflict_reason": str(raw.get("relation_reason") or "同一适用范围内存在冲突")})
            if source_text:
                record["source_text"] = source_text
                record["source_texts"] = [{"text": record["source_text"],
                                           "history_id": unit["history_id"],
                                           "artifact_path": unit["path"]}]
            files.write(layer, record)
        files.rebuild_indexes()

    async def recover(self) -> int:
        count = 0
        if not self.workspace_dir.exists():
            return count
        for project_dir in self.workspace_dir.iterdir():
            if not project_dir.is_dir():
                continue
            store = DocumentHistoryStore(project_dir)
            store.interrupt_collecting()
            for history_id in store.list_analyzable():
                try:
                    await self.analyze(project_dir.name, history_id)
                except Exception as exc:
                    print(f"[history] recovery failed: {history_id}: {exc}", flush=True)
                count += 1
        return count
