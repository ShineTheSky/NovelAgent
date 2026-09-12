"""Turn immutable traces into evidence, time-sensitive memories, and project rules."""

import json
import re

from novelagent.trace.store import TraceStore


class PostTurnAnalyzer:
    """A single asynchronous lifecycle: Trace evidence -> Memory -> Rule."""

    def __init__(self, llm_client, working_dir: str, store: TraceStore | None = None, embedding_gate=None,
                 materializer=None):
        self.llm = llm_client
        self.working_dir = working_dir
        self.store = store or TraceStore()
        self.embedding_gate = embedding_gate
        self.materializer = materializer

    async def analyze(self, trace_id: str, project_id: str, branch_messages: list[dict] | None = None,
                      tools: list[dict] | None = None) -> None:
        try:
            trace = await self.store.get_trace(trace_id)
            if trace is None or trace.get("analysis_status") == "skipped":
                return
            events = await self.store.list_events(trace_id)
            if self.embedding_gate:
                user_inputs = [
                    str(event.get("payload", {}).get("content", ""))
                    for event in events if event["event_type"] == "user_message"
                ]
                gate = await self.embedding_gate.evaluate(user_inputs)
                if gate.skip:
                    await self.store.set_trace_operation(trace_id, "routine", "skipped")
                    return
            memories = await self.store.list_memories(project_id, limit=100, include_rules=True, include_trace=True)
            pending = await self.store.list_pending_evidence(project_id, limit=80)
            result = await self._classify(events, memories, pending, branch_messages, tools)
            valid_events = {event["event_id"] for event in events}
            classification = result.get("classification", {})
            await self.store.save_trace_classification(
                trace_id,
                self._validated_classification_items(
                    classification.get("items", []) if isinstance(classification, dict) else [], valid_events
                ),
            )
            known_memories = {memory["memory_id"]: memory for memory in memories}
            known_evidence = {evidence["evidence_id"] for evidence in pending}
            for item in result.get("memories", []):
                await self._persist_memory(trace_id, project_id, item, known_memories, known_evidence, valid_events)
            await self.store.set_trace_analysis_status(trace_id, "complete")
        except Exception as exc:
            # This branch must never affect a completed user-facing turn.
            print(f"[trace_analyzer] trace={trace_id} failed: {exc}", flush=True)

    async def _classify(self, events: list[dict], memories: list[dict], pending: list[dict],
                        branch_messages: list[dict] | None = None, tools: list[dict] | None = None) -> dict:
        relevant = []
        for event in events:
            if event["event_type"] not in {"user_message", "user_answer", "assistant_turn", "tool_result", "error"}:
                continue
            payload = event.get("payload", {})
            relevant.append({
                "event_id": event["event_id"], "type": event["event_type"], "actor": event["actor"],
                "content": str(payload.get("content") or payload.get("data") or payload.get("message") or "")[:1600],
            })
        memory_context = [{
            "memory_id": memory["memory_id"], "kind": memory["kind"], "subtype": memory["subtype"],
            "claim": memory["claim"], "scope": memory["scope"], "importance": memory["importance"],
            "support_count": memory["support_count"], "status": memory["status"],
        } for memory in memories]
        evidence_context = [{
            "evidence_id": evidence["evidence_id"], "kind": evidence["kind"], "subtype": evidence["subtype"],
            "claim": evidence["claim"], "scope": evidence["scope"], "signal": evidence["signal"],
        } for evidence in pending]
        message_range = f"0–{len(branch_messages) - 1}" if branch_messages else "仅 Trace 事件"
        prompt = f"""[后台记忆分支命令]
你从主 Agent 对话末尾分叉。不要回答或续写主对话，不调用工具，只做记忆判断。
会话轨迹范围为 {message_range}。当前 Trace 有 {len(relevant)} 条可分析事件。先逐条标注 error（错误）、correction（修正意见）、confirmation（明确肯定）、feedback（评价/改进反馈）；同一事件可多项。

只提取可长期复用的 preference（稳定偏好/工作流约束）或 issue（用户指出的错误/反复问题）。普通闲聊、一次性任务、项目剧情事实、模型推测都不能成为记忆。每条必须引用本 Trace 的 event_id。

每条候选首先是 Trace 证据：signal=strong 表示用户明确且可长期适用的规则、纠错或约束，可直接形成单条 Memory；signal=weak 表示单次不够确定，只保留为证据。若至少一条“待合并弱证据”表达同一规律，填写 related_evidence_ids，程序会合成 Memory。
已有 Trace 层证据、Memory 或 Rule 相似时 relation=support 并填写 memory_id，程序会刷新时间、提高重要性；Trace 层会重新激活为 Memory，达标后自动晋升为项目 Rule。新反馈明确否定已有 Memory/Rule 时 relation=contradict 并填写 memory_id；Trace 层只可 support 或 unrelated。Rule 会立即降级，不能再注入。无关为 unrelated，memory_id 留空。只能引用提供的 ID，不得编造。
target_agents 仅填真正应遵循它的 agent（reviewer、chapter_writer、polisher）。when/then 是简短可执行条件和行为，没有必要就留空。
issue subtype 仅限 logic_error、context_error、execution_error、interpretation_error、quality_issue、attempt_error、system_error；preference subtype 可使用 writing_style、dialogue、pacing、plot、character、workflow、constraints。

只输出 JSON，不要 Markdown：
{{"classification":{{"items":[{{"event_id":"evt","type":"error|correction|confirmation|feedback","summary":"简短说明","confidence":0.0}}]}},"memories":[{{"kind":"preference|issue","subtype":"...","claim":"规范化的简短陈述","scope":"global|project|arc|chapter|scene","confidence":0.0,"source_event_ids":["evt"],"signal":"strong|weak","relation":"support|contradict|unrelated","memory_id":"已有 memory_id 或空字符串","related_evidence_ids":["已有 evidence_id"],"target_agents":["reviewer"],"when":"条件","then":"行为"}}]}}

Trace events:\n{json.dumps(relevant, ensure_ascii=False)}
Existing memories and rules:\n{json.dumps(memory_context, ensure_ascii=False)}
Pending weak evidence:\n{json.dumps(evidence_context, ensure_ascii=False)}"""
        text = ""
        messages = [*branch_messages, {"role": "user", "content": prompt}] if branch_messages else [{"role": "user", "content": prompt}]
        async for chunk in self.llm.chat(
            position="main_loop" if branch_messages else "auto_memory", messages=messages,
            tools=tools if branch_messages else None, stream=False,
            tag=":memory-branch" if branch_messages else ":trace-analyzer", max_tokens=128000,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        data = self._parse_json(text)
        return data if isinstance(data, dict) else {"memories": []}

    @staticmethod
    def _parse_json(text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end < start:
            return {"memories": []}
        data = json.loads(clean[start:end + 1])
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _validated_classification_items(items: object, valid_events: set[str]) -> list[dict]:
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            event_id, item_type = str(item.get("event_id", "")), str(item.get("type", ""))
            if event_id not in valid_events or item_type not in {"error", "correction", "confirmation", "feedback"}:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            result.append({"event_id": event_id, "type": item_type,
                           "summary": str(item.get("summary", "")).strip()[:500], "confidence": confidence})
        return result

    async def _persist_memory(self, trace_id: str, project_id: str, item: object, known_memories: dict[str, dict],
                              known_evidence: set[str], valid_events: set[str]) -> None:
        if not isinstance(item, dict) or str(item.get("kind", "")) not in {"preference", "issue"}:
            return
        claim = str(item.get("claim", "")).strip()[:1000]
        event_ids = [str(value) for value in item.get("source_event_ids", []) if str(value) in valid_events]
        if not claim or not event_ids:
            return
        kind, subtype = str(item["kind"]), str(item.get("subtype", ""))[:100]
        scope = str(item.get("scope", "project"))[:50]
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        signal = "strong" if item.get("signal") == "strong" else "weak"
        agents = [str(agent)[:80] for agent in item.get("target_agents", []) if str(agent).strip()][:8]
        when_text, then_text = str(item.get("when", ""))[:300], str(item.get("then", ""))[:500]
        evidence_id = await self.store.record_memory_evidence(
            project_id, trace_id, event_ids, kind, subtype, claim, scope, signal, confidence
        )
        memory_id, relation = str(item.get("memory_id", "")), str(item.get("relation", "unrelated"))
        if memory_id in known_memories and relation == "contradict":
            updated = await self.store.contradict_memory(memory_id, [evidence_id])
            await self._materialize(updated)
            return
        if memory_id in known_memories and relation == "support":
            updated = await self.store.reinforce_memory(memory_id, [evidence_id], 35 if signal == "strong" else 15,
                                                        agents, when_text, then_text)
            await self._materialize(updated)
            return
        if signal == "strong":
            memory_id = await self.store.create_memory(
                project_id, trace_id, event_ids, kind, subtype, claim, scope, confidence,
                importance=65, target_agents=agents, when_text=when_text, then_text=then_text,
            )
            await self.store.attach_evidence_to_memory([evidence_id], memory_id)
            await self._materialize(await self.store.get_memory(project_id, memory_id))
            return
        related = [str(value) for value in item.get("related_evidence_ids", []) if str(value) in known_evidence]
        if related:
            memory_id = await self.store.create_memory(
                project_id, trace_id, event_ids, kind, subtype, claim, scope, confidence,
                importance=min(75, 15 * (len(related) + 1)), support_count=len(related) + 1,
                target_agents=agents, when_text=when_text, then_text=then_text,
            )
            await self.store.attach_evidence_to_memory([*related, evidence_id], memory_id)
            await self._materialize(await self.store.get_memory(project_id, memory_id))

    async def _materialize(self, memory: dict | None) -> None:
        if not memory or not self.materializer:
            return
        try:
            path = self.materializer.sync(memory)
            await self.store.set_memory_file_path(memory["memory_id"], path)
        except OSError as exc:
            print(f"[trace_analyzer] memory materialize failed: {exc}", flush=True)


class PreferenceContextProvider:
    def __init__(self, store: TraceStore | None = None):
        self.store = store or TraceStore()

    async def render(self, project_id: str, limit: int = 8) -> str:
        rules = await self.store.list_rules(project_id, limit=limit)
        lines = []
        for rule in rules:
            action = f"；当{rule['when_text']}时，{rule['then_text']}" if rule.get("when_text") and rule.get("then_text") else ""
            lines.append(f"- {rule['claim']}{action}")
        return "\n".join(lines)
