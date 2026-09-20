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


def sanitize_payload(value):
    """Use the same redaction policy for deferred and immediate Trace events."""
    return _sanitize(value)


class TraceRecorder:
    def __init__(self, store: TraceStore | None = None):
        self.store = store or TraceStore()
        self._states: dict[str, dict] = {}

    async def start(self, session_id: str, project_id: str, user_message: str) -> str:
        trace_id = await self.store.create_trace(
            session_id, project_id, user_message, source_agent="main_agent", agent_position="main_loop",
        )
        self._states[trace_id] = {"sequence": 0, "lock": asyncio.Lock()}
        await self.record(trace_id, "user_message", "user", {"content": user_message})
        return trace_id

    async def start_continuation(self, session_id: str, project_id: str, previous_trace_id: str,
                                 messages: list[dict], reason: str = "context_compression") -> str:
        """Start a new main Trace after the active context has been replaced."""
        latest_user_content = next((
            str(message.get("content", "")).strip()
            for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
            and not str(message.get("content", "")).startswith("[上下文压缩]")
        ), "continuation")
        trace_id = await self.store.create_trace(
            session_id, project_id, f"[{reason}] {latest_user_content[:200]}",
            source_agent="main_agent", agent_position="main_loop",
        )
        await self.store.link_trace_successor(previous_trace_id, trace_id)
        self._states[trace_id] = {"sequence": 0, "lock": asyncio.Lock()}
        await self.record(trace_id, "trace_continued", "system", {
            "previous_trace_id": previous_trace_id, "reason": reason,
        })
        chunk_size = 12_000
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = str(message.get("content", ""))
            chunks = [content[index:index + chunk_size] for index in range(0, len(content), chunk_size)] or [""]
            metadata = {key: value for key, value in message.items() if key != "content"}
            for chunk_index, chunk in enumerate(chunks):
                await self.record(trace_id, "context_snapshot_message", "system", {
                    "message_index": message_index, "chunk_index": chunk_index,
                    "chunk_count": len(chunks), "content": chunk, **metadata,
                })
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
        await self.record(trace_id, "trace_finished", "system", {
            "status": status,
            "final_answer_length": len(final_answer),
            "token_count": token_count,
        })
        await self.store.finish_trace(trace_id, status, final_answer, token_count)
        self._states.pop(trace_id, None)
