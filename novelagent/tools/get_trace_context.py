"""Bounded Trace context lookup for the background Trace analyzer."""

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult
from novelagent.trace.store import TraceStore


class GetTraceContextTool(ToolProtocol):
    name = "GetTraceContext"
    description = (
        "读取当前 Session 内的历史 Trace 上下文。仅在当前证据不足以判断冲突、"
        "合并或修改方向时调用；一次最多读取 3 条 Trace。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "trace_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
                "description": "需要回查的 Trace ID；目标必须属于当前 Session。",
            },
        },
        "required": ["trace_ids"],
    }

    def __init__(self, store: TraceStore, session_id: str,
                 preferred_sources: dict[str, list[str]] | None = None):
        self.store = store
        self.session_id = session_id
        self.preferred_sources = {
            str(trace_id): list(dict.fromkeys(str(event_id) for event_id in event_ids if event_id))
            for trace_id, event_ids in (preferred_sources or {}).items() if trace_id
        }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        raw_ids = params.get("trace_ids", [])
        if not isinstance(raw_ids, list):
            return ToolResult(success=False, error="trace_ids 必须是数组")
        trace_ids = list(dict.fromkeys(str(trace_id) for trace_id in raw_ids if trace_id))
        if not trace_ids or len(trace_ids) > 3:
            return ToolResult(success=False, error="一次必须读取 1 到 3 条 Trace")
        traces = []
        for trace_id in trace_ids:
            trace = await self.store.get_project_trace(context.project_id, trace_id)
            if not trace:
                return ToolResult(success=False, error=f"Trace 不存在或不属于当前项目: {trace_id}")
            if str(trace.get("session_id", "")) != self.session_id:
                return ToolResult(success=False, error=f"Trace 不属于当前 Session: {trace_id}")
            source_event_ids = self.preferred_sources.get(trace_id, [])
            if source_event_ids:
                events = await self.store.get_trace_event_window(
                    trace_id, source_event_ids, before=2, after=2,
                )
            else:
                events = await self.store.list_events(trace_id, limit=40)
            traces.append({
                "trace_id": trace_id,
                "events": [self._event_summary(event) for event in events if self._keep_event(event)],
            })
        return ToolResult(success=True, data={"traces": traces})

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW

    @staticmethod
    def _keep_event(event: dict) -> bool:
        event_type = str(event.get("event_type", ""))
        return event_type in {"user_message", "assistant_turn", "error", "tool_call", "tool_result"} \
            or "tool_" in event_type or event_type.endswith("subagent_done")

    @staticmethod
    def _event_summary(event: dict) -> dict:
        payload = event.get("payload", {}) or {}
        result = {
            "event_id": event["event_id"],
            "trace_id": event.get("trace_id", ""),
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
        if isinstance(payload.get("revision_events"), list):
            result["revision_events"] = payload["revision_events"]
        return result
