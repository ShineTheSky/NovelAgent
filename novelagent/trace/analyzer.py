"""LLM-driven conversion of traces into atomic memories and cross-memory patterns."""

import json
import re

from novelagent.trace.store import TraceStore


class PostTurnAnalyzer:
    """LLM 提取候选记忆；程序校验后将其持久化到 SQLite。"""

    def __init__(self, llm_client, working_dir: str, store: TraceStore | None = None):
        self.llm = llm_client
        self.working_dir = working_dir
        self.store = store or TraceStore()

    async def analyze(
        self,
        trace_id: str,
        project_id: str,
        branch_messages: list[dict] | None = None,
        tools: list[dict] | None = None,
    ) -> None:
        try:
            events = await self.store.list_events(trace_id)
            patterns = await self.store.list_patterns(project_id)
            for pattern in patterns:
                summary = await self.store.get_pattern_summary(pattern["pattern_id"])
                if summary:
                    pattern["summary"] = summary["summary"]
            result = await self._classify(events, patterns, branch_messages, tools)
            valid_event_ids = {event["event_id"] for event in events}
            classification = result.get("classification", {})
            await self.store.save_trace_classification(
                trace_id,
                self._validated_classification_items(
                    classification.get("items", []) if isinstance(classification, dict) else [],
                    valid_event_ids,
                ),
            )
            for item in result.get("memories", []):
                await self._persist_memory(trace_id, project_id, item, patterns, valid_event_ids)
        except Exception as exc:
            # Analysis must never affect the user-facing request path.
            print(f"[trace_analyzer] trace={trace_id} failed: {exc}", flush=True)

    async def _classify(
        self,
        events: list[dict],
        patterns: list[dict],
        branch_messages: list[dict] | None = None,
        tools: list[dict] | None = None,
    ) -> dict:
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
            "status": p["status"], "summary": p.get("summary", ""),
        } for p in patterns]
        message_range = f"0–{len(branch_messages) - 1}" if branch_messages else "仅 Trace 事件"
        prompt = f"""[后台记忆分支命令]
你正在从主 Agent 对话的末尾分叉。不要回答或续写主对话，不要调用任何工具，只执行记忆提取。
当前会话消息轨迹范围为 {message_range}，当前 Trace 包含 {len(relevant)} 条可分析事件。请结合上方完整对话理解语境，仅根据下列 Trace 事件提取可长期复用的原子记忆。

将内容分为：project_fact（项目事实/已确认创作决策）、preference（用户稳定要求）、issue（用户指出的错误或反复执行问题）。
issue 的 subtype 只能是 logic_error、context_error、execution_error、interpretation_error、quality_issue、attempt_error、system_error。
preference 的 subtype 可使用 writing_style、dialogue、pacing、plot、character、workflow、constraints。
不要把普通闲聊、一次性任务或模型自己的推测当记忆。每条必须引用输入中的 event_id。

自动提取的记忆属于 candidate 候选层。对 preference 或 issue，请判断其与候选 Pattern 的关系：duplicate（内容重复）、support（补充支持）、contradict（相互矛盾）、unrelated（无关）。
duplicate、support 或 contradict 时填写候选 Pattern 的 cluster_id；unrelated 时 cluster_id 为空。不要把“主题相关”误判为重复。
先逐条标注事件是否包含：error（事实或执行错误）、correction（用户修正意见）、confirmation（用户明确肯定或确认）、feedback（评价或改进反馈）。同一事件可有多个类型。

仅输出 JSON，不要 Markdown：
{{"classification":{{"items":[{{"event_id":"evt","type":"error|correction|confirmation|feedback","summary":"简短说明","confidence":0.0}}]}},"memories":[{{"kind":"preference|issue|project_fact","subtype":"...","dimension":"...","claim":"规范化的简短陈述","scope":"global|project|arc|chapter|scene","confidence":0.0,"source_event_ids":["evt"],"relation":"duplicate|support|contradict|unrelated","cluster_id":"候选 ID 或空字符串","summary":"一行摘要","content":"可选的详细记忆"}}]}}

Trace events:\n{json.dumps(relevant, ensure_ascii=False)}

Candidate patterns:\n{json.dumps(candidates, ensure_ascii=False)}"""
        text = ""
        messages = [*branch_messages, {"role": "user", "content": prompt}] if branch_messages else [{"role": "user", "content": prompt}]
        async for chunk in self.llm.chat(
            position="main_loop" if branch_messages else "auto_memory",
            messages=messages,
            tools=tools if branch_messages else None,
            stream=False,
            tag=":memory-branch" if branch_messages else ":trace-analyzer",
            max_tokens=128000,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        data = self._parse_json(text)
        return data if isinstance(data, dict) else {"memories": []}

    def _parse_json(self, text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end < start:
            return {"memories": []}
        data = json.loads(clean[start:end + 1])
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _validated_classification_items(items: object, valid_event_ids: set[str]) -> list[dict]:
        if not isinstance(items, list):
            return []
        valid_types = {"error", "correction", "confirmation", "feedback"}
        result: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id", ""))
            item_type = str(item.get("type", ""))
            if event_id not in valid_event_ids or item_type not in valid_types:
                continue
            summary = str(item.get("summary", "")).strip()[:500]
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            result.append({"event_id": event_id, "type": item_type, "summary": summary, "confidence": confidence})
        return result

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

        if kind == "project_fact":
            return
        relation = str(item.get("relation", item.get("action", "unrelated")))
        requested_id = str(item.get("cluster_id", ""))
        valid_ids = {pattern["pattern_id"] for pattern in known_patterns}
        if relation in {"duplicate", "support", "attach", "contradict"} and requested_id in valid_ids:
            pattern_id = requested_id
        else:
            relation = "unrelated"
            pattern_id = await self.store.create_pattern(
                project_id, kind, subtype, str(item.get("dimension", ""))[:100], claim, scope, confidence
            )
        await self.store.attach_memory(pattern_id, memory_id, "contradict" if relation == "contradict" else "support")
        pattern = await self.store.get_pattern(pattern_id)
        if relation in {"duplicate", "support", "attach"} and pattern and pattern["support_count"] >= self._pattern_threshold(pattern):
            await self._consolidate_pattern(pattern_id, trace_id)
        await self._review_if_ready(pattern_id)

    @staticmethod
    def _pattern_threshold(pattern: dict) -> int:
        return 2 if pattern["kind"] == "issue" else 5

    async def _consolidate_pattern(self, pattern_id: str, current_trace_id: str) -> None:
        pattern = await self.store.get_pattern(pattern_id)
        if not pattern:
            return
        existing = await self.store.get_pattern_summary(pattern_id)
        memories = await self.store.list_pattern_memories(pattern_id)
        trace_events = await self.store.list_events(current_trace_id, limit=80)
        evidence = [
            {"memory_id": item["memory_id"], "relation": item["relation"], "claim": item["claim"], "trace_id": item["trace_id"]}
            for item in memories[-20:]
        ]
        prompt = f"""整理自动提取的候选记忆模式。当前 Trace 是新增证据；既有摘要来自此前已整理的 Trace。
不要丢失冲突信息，也不要把一次性意见提升为长期规则。仅输出 JSON：
{{"canonical_claim":"不超过 300 字的宏观规则或问题","summary":"不超过 1000 字的合并摘要"}}

Pattern: {json.dumps(pattern, ensure_ascii=False)}
Existing summary: {json.dumps(existing or {}, ensure_ascii=False)}
Current trace events: {json.dumps(trace_events, ensure_ascii=False)}
Evidence: {json.dumps(evidence, ensure_ascii=False)}"""
        text = ""
        async for chunk in self.llm.chat(
            position="auto_memory",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
            tag=":memory-consolidation",
            max_tokens=2048,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        try:
            result = self._parse_json(text)
        except Exception:
            return
        claim = str(result.get("canonical_claim", "")).strip()[:300]
        summary = str(result.get("summary", "")).strip()[:1000]
        if not claim or not summary:
            return
        await self.store.update_pattern_claim(pattern_id, claim)
        await self.store.save_pattern_summary(pattern_id, summary, [item["memory_id"] for item in memories[-50:]])

    async def _review_if_ready(self, pattern_id: str) -> None:
        pattern = await self.store.get_pattern(pattern_id)
        threshold = self._pattern_threshold(pattern) if pattern else 5
        if not pattern or (pattern["support_count"] < threshold and pattern["contradiction_count"] == 0):
            return
        memories = await self.store.list_pattern_memories(pattern_id)
        evidence = [{"relation": item["relation"], "claim": item["claim"], "scope": item["scope"], "confidence": item["confidence"]} for item in memories]
        prompt = f"""审核以下跨 Trace 的记忆模式。仅输出 JSON：{{"status":"confirmed|tentative|disputed","confidence":0.0}}。
confirmed 仅用于至少 {threshold} 条独立支持且不存在实质冲突的稳定规律；冲突时为 disputed；否则 tentative。
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
