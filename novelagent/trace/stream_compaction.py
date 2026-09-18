"""Compaction helpers for streamed thinking and text deltas."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable


TRACE_STREAM_TYPES = {
    "subagent_thinking", "subagent_text_delta",
    "workflow_thinking", "workflow_text_delta",
}


def stream_text_field(event_type: str) -> str | None:
    if event_type.endswith("_thinking"):
        return "content"
    if event_type.endswith("_text_delta"):
        return "delta"
    return None


def stream_signature(event_type: str, actor: str, parent_event_id: str | None, payload: dict) -> tuple | None:
    field = stream_text_field(event_type)
    if event_type not in TRACE_STREAM_TYPES or field is None:
        return None
    metadata = {key: value for key, value in payload.items() if key not in {field, "stream_chunk_count"}}
    return event_type, actor, parent_event_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def merge_stream_payload(target: dict, incoming: dict, event_type: str) -> None:
    field = stream_text_field(event_type)
    if field is None:
        return
    target[field] = str(target.get(field, "")) + str(incoming.get(field, ""))
    target["stream_chunk_count"] = int(target.get("stream_chunk_count", 1)) + int(
        incoming.get("stream_chunk_count", 1)
    )


class TraceStreamBuffer:
    """Buffer consecutive streaming events while preserving structural boundaries."""

    def __init__(self, record: Callable[..., Awaitable[str]]):
        self.record = record
        self.pending: dict | None = None

    async def add(self, event_type: str, actor: str, payload: dict,
                  parent_event_id: str | None = None) -> None:
        signature = stream_signature(event_type, actor, parent_event_id, payload)
        if signature is None:
            await self.flush()
            await self.record(event_type, actor, payload, parent_event_id)
            return
        if self.pending and self.pending["signature"] == signature:
            merge_stream_payload(self.pending["payload"], payload, event_type)
            return
        await self.flush()
        self.pending = {
            "signature": signature,
            "event_type": event_type,
            "actor": actor,
            "payload": copy.deepcopy(payload),
            "parent_event_id": parent_event_id,
        }

    async def flush(self) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, None
        await self.record(
            pending["event_type"], pending["actor"], pending["payload"], pending["parent_event_id"],
        )


def compact_display_events(events: list[dict]) -> list[dict]:
    """Compact persisted SSE events without changing live streaming behavior."""
    compacted: list[dict] = []
    previous_signature: tuple | None = None
    for original in events:
        event = copy.deepcopy(original)
        event_type = str(event.get("type", ""))
        data = event.get("data")
        field = "content" if event_type == "thinking" else "delta" if event_type == "text_delta" else None
        if field is None or not isinstance(data, dict):
            compacted.append(event)
            previous_signature = None
            continue
        metadata = {key: value for key, value in data.items() if key not in {field, "stream_chunk_count"}}
        signature = event_type, json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
        if compacted and previous_signature == signature:
            previous_data = compacted[-1]["data"]
            previous_data[field] = str(previous_data.get(field, "")) + str(data.get(field, ""))
            previous_data["stream_chunk_count"] = int(previous_data.get("stream_chunk_count", 1)) + int(
                data.get("stream_chunk_count", 1)
            )
        else:
            compacted.append(event)
        previous_signature = signature
    return compacted


def compact_trace_snapshot_events(events: list[dict]) -> list[dict]:
    """Compact the Trace copies stored with a session turn."""
    compacted: list[dict] = []
    previous_signature: tuple | None = None
    for original in events:
        event = copy.deepcopy(original)
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        signature = stream_signature(
            event_type,
            str(event.get("actor", "")),
            event.get("parent_event_id"),
            payload,
        )
        if compacted and signature is not None and previous_signature == signature:
            target = compacted[-1]
            merge_stream_payload(target["payload"], payload, event_type)
            if event.get("duration_ms") is not None:
                target["duration_ms"] = (target.get("duration_ms") or 0) + event["duration_ms"]
        else:
            compacted.append(event)
        previous_signature = signature
    return compacted
