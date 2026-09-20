"""Reusable Trace lifecycle for background and specialist Agent runs."""

from __future__ import annotations

from novelagent.trace.recorder import sanitize_payload


class AgentRunTrace:
    """Buffer one Agent execution and persist it as a normal, linked Trace."""

    def __init__(self, store, trace_id: str, actor: str):
        self.store = store
        self.trace_id = trace_id
        self.actor = actor
        self.sequence = 1
        self.events: list[dict] = []
        self.persisted = 0
        self.finished = False

    @classmethod
    async def start(cls, store, *, session_id: str, project_id: str, actor: str,
                    position: str, source_trace_id: str = "", title: str = ""):
        trace_id = await store.create_trace(
            session_id or f"agent:{actor}", project_id or "__agent_runs__",
            title or f"[{actor}] {position}", source_agent=actor, agent_position=position,
        )
        await store.set_trace_operation(trace_id, "agent_run", "skipped")
        await store.append_event(trace_id, 1, "agent_started", actor, sanitize_payload({
            "actor": actor, "position": position, "source_trace_id": source_trace_id,
        }))
        return cls(store, trace_id, actor)

    @classmethod
    async def try_start(cls, store, **kwargs):
        """Tracing is observability and must never prevent the Agent from running."""
        try:
            return await cls.start(store, **kwargs)
        except Exception as exc:
            print(f"[agent_trace] start failed: {exc}", flush=True)
            return None

    def add(self, event_type: str, payload: dict, actor: str | None = None,
            duration_ms: float | None = None) -> None:
        event_actor = actor or self.actor
        if event_type in {"thinking", "text_delta"} and self.events:
            previous = self.events[-1]
            previous_content = str((previous.get("payload") or {}).get("content", ""))
            incoming_content = str(payload.get("content", ""))
            if (
                previous.get("event_type") == event_type
                and previous.get("actor") == event_actor
                and len(previous_content) + len(incoming_content) <= 12_000
            ):
                previous["payload"]["content"] = previous_content + incoming_content
                previous["payload"]["stream_chunk_count"] = int(
                    previous["payload"].get("stream_chunk_count", 1)
                ) + 1
                return
        self.events.append({
            "event_type": event_type, "actor": event_actor,
            "payload": payload, "duration_ms": duration_ms,
        })

    def add_request(self, *, position: str, tag: str, messages: list[dict],
                    tools: list[dict] | None = None) -> None:
        self.add("llm_request", {
            "position": position, "tag": tag, "message_count": len(messages),
            "tool_count": len(tools or []),
        })
        chunk_size = 12_000
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = str(message.get("content", ""))
            chunks = [content[index:index + chunk_size] for index in range(0, len(content), chunk_size)] or [""]
            metadata = {key: value for key, value in message.items() if key != "content"}
            for chunk_index, chunk in enumerate(chunks):
                self.add("agent_request_message", {
                    "message_index": message_index, "chunk_index": chunk_index,
                    "chunk_count": len(chunks), "content": chunk, **metadata,
                }, actor="system")
        for tool_index, tool in enumerate(tools or []):
            self.add("agent_tool_schema", {"tool_index": tool_index, "schema": tool}, actor="system")

    async def flush(self) -> None:
        for event in self.events[self.persisted:]:
            self.sequence += 1
            await self.store.append_event(
                self.trace_id, self.sequence, event["event_type"], event["actor"],
                sanitize_payload(event["payload"]), duration_ms=event.get("duration_ms"),
            )
        self.persisted = len(self.events)

    async def finish(self, status: str, final_answer: str = "", error: str = "") -> None:
        if self.finished:
            return
        await self.flush()
        self.sequence += 1
        await self.store.append_event(self.trace_id, self.sequence, "agent_finished", "system", {
            "status": status, "error": str(error)[:16_000],
        })
        await self.store.finish_trace(self.trace_id, status, final_answer, 0)
        self.finished = True
