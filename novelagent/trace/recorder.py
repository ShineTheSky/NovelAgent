"""Append-only, redacted Trace event recorder."""

import asyncio

from novelagent.trace.store import TraceStore


_SECRET_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token", "access_token", "refresh_token"}


def _is_secret_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith("_api_key") or normalized.endswith("_secret")


def _sanitize(value, key: str = ""):
    if key == "attachments" and isinstance(value, list):
        # 子 Agent 附件可能包含整章原文；Trace 只保留可复盘的引用摘要。
        return [
            {"path": item.get("path", ""), "content_length": len(str(item.get("content", "")))}
            if isinstance(item, dict) else {"content_length": len(str(item))}
            for item in value
        ]
    if isinstance(value, dict):
        return {item_key: "[REDACTED]" if _is_secret_key(item_key) else _sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, str) and len(value) > 16_000:
        return value[:16_000] + "\n[TRACE_PAYLOAD_TRUNCATED]"
    return value


class TraceRecorder:
    def __init__(self, store: TraceStore | None = None):
        self.store = store or TraceStore()
        self._states: dict[str, dict] = {}

    async def start(self, session_id: str, project_id: str, user_message: str) -> str:
        trace_id = await self.store.create_trace(session_id, project_id, user_message)
        self._states[trace_id] = {"sequence": 0, "lock": asyncio.Lock()}
        await self.record(trace_id, "user_message", "user", {"content": user_message})
        return trace_id

    async def record(self, trace_id: str, event_type: str, actor: str, payload: dict | None = None,
                     parent_event_id: str | None = None, duration_ms: float | None = None) -> str:
        state = self._states.setdefault(trace_id, {"sequence": 0, "lock": asyncio.Lock()})
        async with state["lock"]:
            state["sequence"] += 1
            return await self.store.append_event(
                trace_id, state["sequence"], event_type, actor, _sanitize(payload or {}), parent_event_id, duration_ms
            )

    async def finish(self, trace_id: str, status: str, final_answer: str = "", token_count: int = 0) -> None:
        await self.store.finish_trace(trace_id, status, final_answer, token_count)
        self._states.pop(trace_id, None)
