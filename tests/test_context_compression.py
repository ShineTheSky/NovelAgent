import asyncio

from novelagent.context.compression import build_cumulative_compression
from novelagent.storage import database
from novelagent.trace.recorder import TraceRecorder
from novelagent.trace.store import TraceStore


def test_compression_uses_all_summaries_since_previous_compression_and_raw_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "compression-summary.db"))

    async def scenario():
        await database.init()
        store = TraceStore()
        recorder = TraceRecorder(store)

        for number in range(1, 3):
            trace_id = await recorder.start("session-a", "project-a", f"用户{number}")
            await recorder.finish(trace_id, "completed", f"回复{number}")
            await store.append_session_turn(
                "session-a", f"用户{number}", f"回复{number}", source_trace_id=trace_id,
            )
        window = await store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "上下文"}],
        )
        await store.complete_trace_window_summary(
            window["window_id"], "第一窗口摘要", "tr_memory_agent_1",
        )

        first = await build_cumulative_compression(store, "session-a")
        await store.save_session_compression_state(
            "session-a", first["last_compressed_turn_no"], first["state_messages"],
        )

        for number in range(3, 5):
            trace_id = await recorder.start("session-a", "project-a", f"用户{number}-完整内容")
            await recorder.finish(trace_id, "completed", f"回复{number}-完整内容")
            await store.append_session_turn(
                "session-a", f"用户{number}-完整内容", f"回复{number}-完整内容",
                source_trace_id=trace_id,
            )
        second_window = await store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "第二窗口"}],
        )
        await store.complete_trace_window_summary(
            second_window["window_id"], "第二窗口摘要", "tr_memory_agent_2",
        )

        trace_id = await recorder.start("session-a", "project-a", "用户5-完整内容")
        await recorder.finish(trace_id, "completed", "回复5-完整内容")
        await store.append_session_turn(
            "session-a", "用户5-完整内容", "回复5-完整内容", source_trace_id=trace_id,
        )
        third_window = await store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "第三窗口"}],
        )
        await store.complete_trace_window_summary(
            third_window["window_id"], "第三窗口摘要", "tr_memory_agent_3",
        )

        trace_id = await recorder.start("session-a", "project-a", "用户6-未总结")
        await recorder.finish(trace_id, "completed", "回复6-未总结")
        await store.append_session_turn(
            "session-a", "用户6-未总结", "回复6-未总结", source_trace_id=trace_id,
        )

        result = await build_cumulative_compression(
            store, "session-a",
            current_user_message="当前尚未完成的用户请求",
        )

        assert result["summary_count"] == 2
        assert result["summary_window_ids"] == [
            second_window["window_id"], third_window["window_id"],
        ]
        assert result["summary_trace_ids"] == ["tr_memory_agent_2", "tr_memory_agent_3"]
        assert result["previous_compression_turn_no"] == 2
        assert result["last_compressed_turn_no"] == 6
        assert result["uncovered_turn_count"] == 1
        assert result["state_messages"] == [
            {"role": "user", "content": "[Memory Summary · Turn 1-2] 第一窗口摘要"},
            {"role": "user", "content": "[Memory Summary · Turn 3-4] 第二窗口摘要"},
            {"role": "user", "content": "[Memory Summary · Turn 5-5] 第三窗口摘要"},
            {"role": "user", "content": "用户6-未总结"},
            {"role": "assistant", "content": "回复6-未总结"},
        ]
        assert result["messages"] == [
            *result["state_messages"],
            {"role": "user", "content": "当前尚未完成的用户请求"},
        ]

    asyncio.run(scenario())


def test_compression_uses_full_legacy_messages_when_turn_table_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "legacy-compression.db"))

    async def scenario():
        await database.init()
        legacy = [
            {"role": "user", "content": "最早的完整设定"},
            {"role": "assistant", "content": "中间的完整回复"},
            {"role": "user", "content": "最后的完整要求"},
        ]
        result = await build_cumulative_compression(
            TraceStore(), "legacy-session", legacy_messages=legacy,
        )

        assert result["summary_count"] == 0
        assert result["messages"] == legacy

    asyncio.run(scenario())
