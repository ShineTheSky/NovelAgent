"""LLM-driven conversion of Trace events into Evidence, Memory and Pattern files."""

import json
import re
from pathlib import Path

from novelagent.memory.memory_manager import MemoryManager
from novelagent.trace.store import TraceStore


class PostTurnAnalyzer:
    """Trace 保留原始事实；LLM 仅提出经过程序校验的文件化记忆关联。"""

    def __init__(self, llm_client, working_dir: str, store: TraceStore | None = None):
        self.llm = llm_client
        self.working_dir = working_dir
        self.store = store or TraceStore()

    async def analyze(self, trace_id: str, project_id: str, tools: list[dict] | None = None) -> None:
        try:
            if not await self.store.get_project_trace(project_id, trace_id):
                return
            events = await self.store.list_events(trace_id, limit=1000)
            if not events:
                return
            manager = MemoryManager(str(Path(self.working_dir) / project_id), self.llm)
            result = await self._classify(events, manager)
            valid_event_ids = {event["event_id"] for event in events}
            classification = result.get("classification", {})
            await self.store.save_trace_classification(
                trace_id,
                self._validated_classification_items(
                    classification.get("items", []) if isinstance(classification, dict) else [],
                    valid_event_ids,
                ),
            )
            evidence = [item for raw in result.get("evidence", []) if (item := self._validated_record(raw, valid_event_ids))]
            memories = [item for raw in result.get("memories", []) if (item := self._validated_record(raw, valid_event_ids))]
            manager.persist_evidence(trace_id, evidence)
            manager.persist_memories(trace_id, memories)
            await self._promote_evidence(manager, project_id, result.get("promote_evidence_ids", []))
            await self._persist_patterns(manager, project_id, result.get("patterns", []))
        except Exception as exc:
            print(f"[trace_analyzer] trace={trace_id} failed: {exc}", flush=True)

    async def _classify(self, events: list[dict], manager: MemoryManager) -> dict:
        relevant = [self._event_for_prompt(event) for event in events if event["event_type"] in {
            "user_message", "user_answer", "assistant_turn", "tool_result", "error", "permission_response",
        }]
        evidence = [self._record_for_prompt(record, "evidence") for record in manager.list_records("evidence")[:30]]
        memories = [self._record_for_prompt(record, "memory") for record in manager.list_records("memory")[:50]]
        patterns = [self._pattern_for_prompt(record) for record in manager.list_records("pattern")[:30]]
        prompt = f"""[后台 Trace 记忆分析]
只分析下面的不可变 Trace 事件；不要续写主对话、不要调用工具。

任务：
1. 标记事件中的 error、correction、confirmation、feedback。
2. 对当前 Trace 中有潜在长期价值但证据不足的线索输出 evidence。不要把一次性要求、普通闲聊或模型推测保存为 evidence。
3. 对当前 Trace 中已有明确确认、可独立复用的结论输出 memories。
4. 仅当既有 Evidence 已获得充分支持时，在 promote_evidence_ids 中列出其 ID；被提升的 Evidence 会被删除并生成 Memory。
5. 仅当至少两条 active Memory 构成稳定模式时输出 patterns。Pattern 的 links 只可引用候选 Memory 列表中的 ID；trace_ids 是你认为应回读的来源 Trace。程序会验证所有引用，编造 ID 会被丢弃。

类型：project_fact（确认项目决策）、preference（稳定用户偏好）、issue（反复出现的问题）。
scope 仅可为 global/project/arc/chapter/scene。
issue subtype 仅可为 logic_error/context_error/execution_error/interpretation_error/quality_issue/attempt_error/system_error。
preference subtype 可为 writing_style/dialogue/pacing/plot/character/workflow/constraints。

只输出 JSON：
{{"classification":{{"items":[{{"event_id":"evt","type":"error|correction|confirmation|feedback","summary":"...","confidence":0.0}}]}},"evidence":[{{"kind":"...","subtype":"...","claim":"...","scope":"...","confidence":0.0,"source_event_ids":["evt"],"summary":"...","content":"..."}}],"memories":[{{"kind":"...","subtype":"...","claim":"...","scope":"...","confidence":0.0,"source_event_ids":["evt"],"summary":"...","content":"..."}}],"promote_evidence_ids":["evd..."],"patterns":[{{"pattern_id":"已有 pat... 或空字符串","kind":"preference|issue","subtype":"...","dimension":"...","claim":"...","scope":"...","confidence":0.0,"summary":"...","content":"...","links":[{{"memory_id":"mem...","relation":"support|contradict"}}],"trace_ids":["tr..."]}}]}}

当前 Trace 事件：
{json.dumps(relevant, ensure_ascii=False)}

既有 Evidence：
{json.dumps(evidence, ensure_ascii=False)}

既有 active Memory：
{json.dumps(memories, ensure_ascii=False)}

既有 Pattern：
{json.dumps(patterns, ensure_ascii=False)}"""
        text = ""
        async for chunk in self.llm.chat(
            position="auto_memory",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
            tag=":trace-analyzer",
            max_tokens=4096,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        return self._parse_json(text)

    async def _promote_evidence(self, manager: MemoryManager, project_id: str, evidence_ids: object) -> None:
        if not isinstance(evidence_ids, list):
            return
        for evidence_id in dict.fromkeys(str(value) for value in evidence_ids if value):
            record = manager.get_record("evidence", evidence_id)
            if not record:
                continue
            trace_id = str(record.get("trace_id", ""))
            source_event_ids = record.get("source_event_ids", [])
            if not trace_id or not await self.store.get_project_trace(project_id, trace_id):
                continue
            events = await self.store.get_events_by_ids(trace_id, source_event_ids)
            if len(events) != len(set(source_event_ids)):
                continue
            manager.promote_evidence(evidence_id)

    async def _persist_patterns(self, manager: MemoryManager, project_id: str, proposals: object) -> None:
        if not isinstance(proposals, list):
            return
        for proposal in proposals:
            candidate = await self._validated_pattern(manager, project_id, proposal)
            if candidate and await self._confirm_pattern_with_trace_windows(manager, candidate):
                manager.create_or_update_pattern(candidate)

    async def _confirm_pattern_with_trace_windows(self, manager: MemoryManager, candidate: dict) -> bool:
        evidence = []
        for link in candidate["memory_links"]:
            memory = manager.get_record("memory", link["memory_id"])
            if not memory:
                return False
            trace_id = str(memory.get("trace_id", ""))
            window = await self.store.get_trace_event_window(trace_id, memory.get("source_event_ids", []), before=2, after=2)
            evidence.append({
                "memory_id": link["memory_id"],
                "relation": link["relation"],
                "claim": memory.get("claim") or memory.get("summary"),
                "trace_id": trace_id,
                "events": [self._event_for_prompt(event) for event in window],
            })
        prompt = f"""复核 Pattern 候选。不要只依据摘要；请根据各 Memory 回读的原始 Trace 事件窗口判断。
若存在范围不同、一次性例外或语义不充分，拒绝创建/更新，以避免误伤。
仅输出 JSON：{{"accept":true|false}}。

Pattern 候选：\n{json.dumps(candidate, ensure_ascii=False)}

Trace 证据窗口：\n{json.dumps(evidence, ensure_ascii=False)}"""
        text = ""
        async for chunk in self.llm.chat(
            position="auto_memory",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
            tag=":pattern-trace-review",
            max_tokens=512,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        return bool(self._parse_json(text).get("accept", False))

    async def _validated_pattern(self, manager: MemoryManager, project_id: str, proposal: object) -> dict | None:
        if not isinstance(proposal, dict):
            return None
        kind = str(proposal.get("kind", ""))
        if kind not in {"preference", "issue"}:
            return None
        claim = str(proposal.get("claim", "")).strip()[:1000]
        links = proposal.get("links", [])
        if not claim or not isinstance(links, list):
            return None
        valid_links, linked_trace_ids = [], []
        seen_memory_ids = set()
        for link in links:
            if not isinstance(link, dict):
                continue
            memory_id = str(link.get("memory_id", ""))
            relation = str(link.get("relation", ""))
            memory = manager.get_record("memory", memory_id)
            if memory is None or memory_id in seen_memory_ids or relation not in {"support", "contradict"}:
                continue
            trace_id = str(memory.get("trace_id", ""))
            source_event_ids = memory.get("source_event_ids", [])
            if not trace_id or not await self.store.get_project_trace(project_id, trace_id):
                continue
            if len(await self.store.get_events_by_ids(trace_id, source_event_ids)) != len(set(source_event_ids)):
                continue
            seen_memory_ids.add(memory_id)
            valid_links.append({"memory_id": memory_id, "relation": relation})
            linked_trace_ids.append(trace_id)
        if len(valid_links) < 2:
            return None
        requested_trace_ids = {str(value) for value in proposal.get("trace_ids", []) if value}
        trace_ids = [trace_id for trace_id in dict.fromkeys(linked_trace_ids) if not requested_trace_ids or trace_id in requested_trace_ids]
        if not trace_ids:
            return None
        return {
            "pattern_id": str(proposal.get("pattern_id", "")),
            "kind": kind,
            "subtype": str(proposal.get("subtype", ""))[:100],
            "dimension": str(proposal.get("dimension", ""))[:100],
            "claim": claim,
            "scope": str(proposal.get("scope", "project")),
            "confidence": self._confidence(proposal.get("confidence", 0.5)),
            "summary": str(proposal.get("summary", "") or claim)[:500],
            "content": str(proposal.get("content", "") or claim)[:5000],
            "memory_links": valid_links,
            "trace_ids": trace_ids,
            "status": "ready_for_review",
        }

    @staticmethod
    def _event_for_prompt(event: dict) -> dict:
        payload = event.get("payload", {})
        return {
            "event_id": event["event_id"],
            "type": event["event_type"],
            "actor": event["actor"],
            "content": str(payload.get("content") or payload.get("data") or payload.get("message") or "")[:1600],
        }

    @staticmethod
    def _record_for_prompt(record: dict, layer: str) -> dict:
        return {
            "id": record.get(f"{layer}_id"),
            "kind": record.get("type"),
            "claim": record.get("claim") or record.get("summary"),
            "scope": record.get("scope"),
            "trace_id": record.get("trace_id"),
            "status": record.get("status"),
        }

    @staticmethod
    def _pattern_for_prompt(record: dict) -> dict:
        return {
            "pattern_id": record.get("pattern_id"),
            "claim": record.get("claim") or record.get("summary"),
            "status": record.get("status"),
            "memory_ids": [*record.get("supporting_memory_ids", []), *record.get("contradicting_memory_ids", [])],
            "trace_ids": record.get("trace_ids", []),
        }

    def _parse_json(self, text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            data = json.loads(clean[start:end + 1])
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _confidence(value: object) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    @classmethod
    def _validated_record(cls, item: object, valid_event_ids: set[str]) -> dict | None:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind", ""))
        claim = str(item.get("claim", "")).strip()[:1000]
        source_event_ids = list(dict.fromkeys(str(value) for value in item.get("source_event_ids", []) if str(value) in valid_event_ids))
        if kind not in {"project_fact", "preference", "issue"} or not claim or not source_event_ids:
            return None
        return {
            "kind": kind,
            "subtype": str(item.get("subtype", ""))[:100],
            "claim": claim,
            "scope": str(item.get("scope", "project")),
            "confidence": cls._confidence(item.get("confidence", 0.5)),
            "source_event_ids": source_event_ids,
            "summary": str(item.get("summary", "") or claim)[:500],
            "content": str(item.get("content", "") or claim).strip()[:5000],
        }

    @classmethod
    def _validated_classification_items(cls, items: object, valid_event_ids: set[str]) -> list[dict]:
        if not isinstance(items, list):
            return []
        valid_types = {"error", "correction", "confirmation", "feedback"}
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            event_id, item_type = str(item.get("event_id", "")), str(item.get("type", ""))
            if event_id not in valid_event_ids or item_type not in valid_types:
                continue
            result.append({
                "event_id": event_id,
                "type": item_type,
                "summary": str(item.get("summary", "")).strip()[:500],
                "confidence": cls._confidence(item.get("confidence", 0.5)),
            })
        return result
