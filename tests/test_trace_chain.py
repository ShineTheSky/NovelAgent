import asyncio

from novelagent.storage import database
from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.recorder import TraceRecorder
from novelagent.trace.store import TraceStore


def test_raw_traces_keep_request_chain_and_window_references(tmp_path, monkeypatch):
    """A five-turn window must reference request traces instead of cloning them."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "trace-chain.db"))

    async def scenario():
        await database.init()
        store = TraceStore()
        recorder = TraceRecorder(store)

        first = await recorder.start("session-a", "project-a", "第一条用户消息")
        await recorder.record(first, "assistant_turn", "main_agent", {"content": "第一条回复"})
        await recorder.finish(first, "completed", "第一条回复")
        await store.append_session_turn("session-a", "第一条用户消息", "第一条回复", source_trace_id=first)

        second = await recorder.start("session-a", "project-a", "第二条用户消息")
        await recorder.record(second, "tool_result", "tool", {"tool": "Read", "success": True})
        await recorder.finish(second, "completed", "第二条回复")
        await store.append_session_turn("session-a", "第二条用户消息", "第二条回复", source_trace_id=second)

        context = await store.get_trace_context(second, before=1, after=1)
        assert context["previous"][0]["trace_id"] == first
        assert context["trace"]["previous_trace_id"] == first
        assert context["previous"][0]["next_trace_id"] == second
        assert any(event["event_type"] == "user_message" for event in context["trace"]["events"])

        window = await store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "上下文"}], reason="interval",
        )
        assert window["source_trace_ids"] == [first, second]
        assert len(await store.list_session_traces("session-a")) == 2
        assert [event["trace_id"] for event in await store.list_trace_window_events(window["window_id"])] == [
            first, first, first, second, second, second,
        ]

        class _LLM:
            calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                yield type("Chunk", (), {"type": "text_delta", "content": '{"items": [], "records": []}'})()

        analyzer = FileTraceAnalyzer(_LLM(), str(tmp_path / "workspace"), store)
        await analyzer.analyze_window(window["window_id"], "project-a")
        saved_window = await store.get_trace_window(window["window_id"])
        assert analyzer.llm.calls == 1
        assert saved_window["status"] == "complete"

    asyncio.run(scenario())
