import asyncio

from novelagent.trace.stream_compaction import (
    TraceStreamBuffer,
    compact_display_events,
    compact_trace_snapshot_events,
)


def test_trace_stream_buffer_merges_only_contiguous_matching_chunks():
    recorded = []

    async def record(event_type, actor, payload, parent_event_id=None):
        recorded.append((event_type, actor, payload, parent_event_id))
        return f"event-{len(recorded)}"

    async def exercise():
        buffer = TraceStreamBuffer(record)
        await buffer.add("subagent_thinking", "subagent:outliner", {"content": "先", "run_id": "run-1"}, "tool-1")
        await buffer.add("subagent_thinking", "subagent:outliner", {"content": "判断", "run_id": "run-1"}, "tool-1")
        await buffer.add("subagent_tool_call", "subagent:outliner", {"tool": "Read"}, "tool-1")
        await buffer.add("subagent_thinking", "subagent:outliner", {"content": "再处理", "run_id": "run-1"}, "tool-1")
        await buffer.flush()

    asyncio.run(exercise())

    assert len(recorded) == 3
    assert recorded[0][2] == {"content": "先判断", "run_id": "run-1", "stream_chunk_count": 2}
    assert recorded[1][0] == "subagent_tool_call"
    assert recorded[2][2]["content"] == "再处理"


def test_trace_snapshot_compaction_respects_run_and_event_boundaries():
    events = [
        {"event_id": "1", "event_type": "subagent_text_delta", "actor": "subagent:writer", "payload": {"delta": "甲", "run_id": "a"}},
        {"event_id": "2", "event_type": "subagent_text_delta", "actor": "subagent:writer", "payload": {"delta": "乙", "run_id": "a"}},
        {"event_id": "3", "event_type": "subagent_text_delta", "actor": "subagent:writer", "payload": {"delta": "丙", "run_id": "b"}},
        {"event_id": "4", "event_type": "tool_result", "actor": "tool", "payload": {"success": True}},
        {"event_id": "5", "event_type": "subagent_text_delta", "actor": "subagent:writer", "payload": {"delta": "丁", "run_id": "b"}},
    ]

    compacted = compact_trace_snapshot_events(events)

    assert [event["event_id"] for event in compacted] == ["1", "3", "4", "5"]
    assert compacted[0]["payload"]["delta"] == "甲乙"
    assert compacted[0]["payload"]["stream_chunk_count"] == 2


def test_display_compaction_keeps_live_event_metadata_boundaries():
    events = [
        {"type": "thinking", "data": {"content": "A", "source": "subagent", "run_id": "one"}},
        {"type": "thinking", "data": {"content": "B", "source": "subagent", "run_id": "one"}},
        {"type": "tool_call", "data": {"tool": "Read"}},
        {"type": "thinking", "data": {"content": "C", "source": "subagent", "run_id": "one"}},
    ]

    compacted = compact_display_events(events)

    assert len(compacted) == 3
    assert compacted[0]["data"]["content"] == "AB"
    assert compacted[0]["data"]["stream_chunk_count"] == 2
    assert compacted[2]["data"]["content"] == "C"
