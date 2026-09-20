"""Bounded Trace context lookup for the background Trace analyzer."""

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult
from novelagent.trace.store import TraceStore


class GetTraceContextTool(ToolProtocol):
    max_turn_span = 12
    name = "GetTraceContext"
    description = (
        "按 ReAct turn 区间读取当前 Session 内的 Trace 上下文。仅在初始局部上下文不足以判断冲突、"
        "合并或修改方向时调用；一次最多读取 3 个区间，每个区间最多 12 个 turn。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                        "start_turn": {"type": "integer", "minimum": 1},
                        "end_turn": {"type": "integer", "minimum": 1},
                    },
                    "required": ["trace_id", "start_turn", "end_turn"],
                },
                "minItems": 1,
                "maxItems": 3,
                "description": "需要补查的 Trace 与闭区间 turn；目标必须属于当前 Session。",
            },
            "trace_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
                "description": "兼容旧调用；未指定 turn 时只返回锚点附近或最后 5 个 turn。",
            },
        },
        "required": ["requests"],
    }

    def __init__(self, store: TraceStore, session_id: str,
                 preferred_sources: dict[str, list[str]] | None = None):
        self.store = store
        self.session_id = session_id
        self.preferred_sources = {
            str(trace_id): list(dict.fromkeys(str(event_id) for event_id in event_ids if event_id))
            for trace_id, event_ids in (preferred_sources or {}).items() if trace_id
        }
        self.last_events: list[dict] = []

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        self.last_events = []
        raw_requests = params.get("requests")
        if raw_requests is not None:
            if not isinstance(raw_requests, list) or not 1 <= len(raw_requests) <= 3:
                return ToolResult(success=False, error="requests 必须包含 1 到 3 个 turn 区间")
            requests = []
            for raw in raw_requests:
                if not isinstance(raw, dict) or not raw.get("trace_id"):
                    return ToolResult(success=False, error="每个请求都必须提供 trace_id")
                try:
                    start_turn, end_turn = int(raw.get("start_turn")), int(raw.get("end_turn"))
                except (TypeError, ValueError):
                    return ToolResult(success=False, error="start_turn 和 end_turn 必须是整数")
                if start_turn < 1 or end_turn < start_turn:
                    return ToolResult(success=False, error="turn 区间无效")
                if end_turn - start_turn + 1 > self.max_turn_span:
                    return ToolResult(success=False, error=f"每个区间最多读取 {self.max_turn_span} 个 turn")
                requests.append({"trace_id": str(raw["trace_id"]), "start_turn": start_turn, "end_turn": end_turn})
        else:
            raw_ids = params.get("trace_ids", [])
            if not isinstance(raw_ids, list):
                return ToolResult(success=False, error="trace_ids 必须是数组")
            trace_ids = list(dict.fromkeys(str(trace_id) for trace_id in raw_ids if trace_id))
            if not trace_ids or len(trace_ids) > 3:
                return ToolResult(success=False, error="一次必须读取 1 到 3 条 Trace")
            requests = [{"trace_id": trace_id} for trace_id in trace_ids]

        traces = []
        for request in requests:
            trace_id = request["trace_id"]
            trace = await self.store.get_project_trace(context.project_id, trace_id)
            if not trace:
                return ToolResult(success=False, error=f"Trace 不存在或不属于当前项目: {trace_id}")
            if str(trace.get("session_id", "")) != self.session_id:
                return ToolResult(success=False, error=f"Trace 不属于当前 Session: {trace_id}")
            if "start_turn" in request:
                events = await self.store.get_trace_turn_range(
                    trace_id, request["start_turn"], request["end_turn"], limit=300,
                )
            else:
                source_event_ids = self.preferred_sources.get(trace_id, [])
                if source_event_ids:
                    events = await self.store.get_trace_event_window(
                        trace_id, source_event_ids, before=2, after=2,
                    )
                else:
                    all_events = TraceStore.annotate_event_turns(await self.store.list_events(trace_id, limit=500))
                    turns = sorted({int(event.get("trace_turn") or 1) for event in all_events})
                    selected_turns = set(turns[-5:])
                    events = [event for event in all_events if int(event.get("trace_turn") or 1) in selected_turns]
            events = TraceStore.annotate_event_turns(events)
            self.last_events.extend(events)
            traces.append({
                "trace_id": trace_id,
                "turn_range": {
                    "start": min((int(event.get("trace_turn") or 1) for event in events), default=None),
                    "end": max((int(event.get("trace_turn") or 1) for event in events), default=None),
                },
                "events": [self._event_summary(event) for event in events if self._keep_event(event)],
            })
        return ToolResult(success=True, data={"traces": traces})

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW

    @staticmethod
    def _keep_event(event: dict) -> bool:
        event_type = str(event.get("event_type", ""))
        return event_type in {"user_message", "user_answer", "question_ask", "assistant_turn", "error", "tool_call", "tool_result"} \
            or "tool_" in event_type or event_type.endswith("subagent_done")

    @staticmethod
    def _event_summary(event: dict) -> dict:
        payload = event.get("payload", {}) or {}
        result = {
            "event_id": event["event_id"],
            "trace_id": event.get("trace_id", ""),
            "turn": event.get("trace_turn", 1),
            "type": event.get("event_type", ""),
        }
        content = payload.get("content") or payload.get("message")
        if content:
            result["content"] = str(content)[:1200]
        if payload.get("tool"):
            result["tool"] = payload["tool"]
        if isinstance(payload.get("params"), dict):
            result["params"] = {
                key: str(value)[:1200]
                for key, value in payload["params"].items()
                if key in {"path", "old_string", "new_string", "expected_revision_id"}
            }
        if payload.get("data"):
            result["data"] = str(payload["data"])[:1400]
        if payload.get("answers"):
            result["answers"] = payload["answers"]
        if payload.get("questions"):
            result["questions"] = payload["questions"]
        if isinstance(payload.get("revision_events"), list):
            result["revision_events"] = payload["revision_events"]
        return result
