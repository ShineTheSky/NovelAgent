import asyncio
import json

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
        assert (await store.validate_trace_chain(first))["valid"] is True
        assert (await store.validate_trace_chain(second))["valid"] is True

        window = await store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "上下文"}], reason="interval",
        )
        assert window["source_trace_ids"] == [first, second]
        assert len(await store.list_session_traces("session-a")) == 2
        assert [event["trace_id"] for event in await store.list_trace_window_events(window["window_id"])] == [
            first, first, first, second, second, second,
        ]

        class _LLM:
            calls = []

            async def chat(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["tag"] == ":trace-fork/trajectory":
                    prompt = kwargs["messages"][-1]["content"]
                    event = json.loads(prompt.split("Raw trace events:", 1)[1])[0]
                    trace_id = event["trace_id"]
                    turn_no = int(event["turn"])
                    payload = {
                        "trajectory": [{
                            "role": "observation",
                            "content": json.dumps(event, ensure_ascii=False),
                            "timestamp": event["timestamp"],
                        }],
                        "provenance": [{
                            "record_index": 0, "trace_id": trace_id,
                            "turn_id": f"{trace_id}:{turn_no}", "turn_no": turn_no,
                            "source_event_ids": [event["event_id"]],
                        }],
                    }
                    yield type("Chunk", (), {"type": "text_delta", "content": json.dumps(payload, ensure_ascii=False)})()
                    return
                yield type("Chunk", (), {"type": "text_delta", "content": '{"window_summary":"当前窗口摘要","items":[],"records":[]}'})()

        analyzer = FileTraceAnalyzer(_LLM(), str(tmp_path / "workspace"), store)
        main_tools = [{"name": "Read", "description": "读取文件", "parameters": {"type": "object", "properties": {}}}]
        await analyzer.analyze_window(window["window_id"], "project-a", _tools=main_tools)
        saved_window = await store.get_trace_window(window["window_id"])
        assert len(analyzer.llm.calls) == 2
        assert analyzer.llm.calls[0]["position"] == "main_loop"
        assert analyzer.llm.calls[0]["messages"][0]["content"] == "上下文"
        assert "Raw trace events:" in analyzer.llm.calls[0]["messages"][-1]["content"]
        assert analyzer.llm.calls[0]["tools"] is None
        assert '"window_summary"' in analyzer.llm.calls[1]["messages"][-1]["content"]
        assert [tool["name"] for tool in analyzer.llm.calls[1]["tools"]] == ["Read", "GetTraceContext"]
        assert saved_window["status"] == "complete"
        assert saved_window["summary"] == "当前窗口摘要"
        assert saved_window["normalized_trajectory"]
        assert saved_window["trajectory_provenance"][0]["trace_id"] == first
        assert saved_window["summary_start_turn_no"] == 1
        assert saved_window["summary_end_turn_no"] == 2

        analyzer.llm.calls.clear()
        await analyzer.analyze_window(window["window_id"], "project-a", _tools=main_tools)
        assert analyzer.llm.calls == []
        assert (await store.get_trace_window(window["window_id"]))["status"] == "complete"

    asyncio.run(scenario())


def test_context_compression_rotates_trace_and_preserves_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "compression-chain.db"))

    async def scenario():
        await database.init()
        store = TraceStore()
        recorder = TraceRecorder(store)

        before = await recorder.start("session-a", "project-a", "触发压缩的用户消息")
        await recorder.record(before, "context_compression", "system", {"before_token_count": 120_000})
        compressed_messages = [
            {"role": "user", "content": "[上下文压缩] 先前内容摘要"},
            {"role": "user", "content": "触发压缩的用户消息"},
        ]
        after = await recorder.start_continuation(
            "session-a", "project-a", before, compressed_messages,
        )
        await recorder.record(before, "context_compression_completed", "system", {
            "next_trace_id": after, "after_token_count": 2_000,
        })
        await recorder.finish(before, "compressed")
        await recorder.record(after, "assistant_turn", "main_agent", {
            "turn": 1, "content": "压缩后产生的新内容",
        })
        await recorder.finish(after, "completed", "压缩后产生的新内容")
        await store.append_session_turn(
            "session-a", "触发压缩的用户消息", "压缩后产生的新内容",
            source_trace_id=after,
        )

        before_trace = await store.get_trace(before)
        after_trace = await store.get_trace(after)
        assert before_trace["next_trace_id"] == after
        assert after_trace["previous_trace_id"] == before
        assert before_trace["turn_no"] == after_trace["turn_no"] == 1
        after_context = await store.get_trace_context(after, before=1, after=1)
        before_context = await store.get_trace_context(before, before=1, after=1)
        assert after_context["previous"][-1]["trace_id"] == before
        assert before_context["next"][0]["trace_id"] == after

        after_events = await store.list_events(after)
        snapshot = [
            event["payload"]["content"] for event in after_events
            if event["event_type"] == "context_snapshot_message"
        ]
        assert "".join(snapshot) == "[上下文压缩] 先前内容摘要触发压缩的用户消息"

        window = await store.capture_pending_trace_window(
            "session-a", "project-a", compressed_messages, reason="compression",
        )
        assert window["source_trace_ids"] == [before, after]
        window_events = await store.list_trace_window_events(window["window_id"])
        assert {event["trace_id"] for event in window_events} == {before, after}

        manual_after = await recorder.start_continuation(
            "session-a", "project-a", after,
            [{"role": "user", "content": "[上下文压缩] 二次摘要"}],
            reason="manual_context_compression",
        )
        await recorder.finish(manual_after, "completed")
        assert await store.replace_latest_turn_trace("session-a", after, manual_after)
        assert await store.latest_session_turn_trace("session-a") == manual_after
        manual_trace = await store.get_trace(manual_after)
        assert manual_trace["previous_trace_id"] == after
        assert manual_trace["turn_no"] == 1

    asyncio.run(scenario())


def test_trace_management_filters_and_validates_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "trace-management.db"))

    async def scenario():
        await database.init()
        store = TraceStore()
        recorder = TraceRecorder(store)

        before = await recorder.start("session-a", "project-a", "需要压缩的消息")
        await recorder.finish(before, "compressed", token_count=120_000)
        after = await recorder.start_continuation(
            "session-a", "project-a", before,
            [{"role": "user", "content": "[上下文压缩] 摘要"}],
        )
        await store.set_trace_operation(after, "context_transition", "pending")
        await recorder.finish(after, "completed", token_count=2_000)
        await store.append_session_turn(
            "session-a", "需要压缩的消息", "压缩后回复", source_trace_id=after,
        )

        page = await store.list_project_traces_page(
            "project-a", search=after[-8:], operation_kind="context_transition",
            analysis_status="pending", source_agent="main_agent", limit=10, offset=0,
        )
        assert page["total"] == 1
        assert page["items"][0]["trace_id"] == after
        assert page["items"][0]["event_count"] >= 2
        assert page["items"][0]["source_agent"] == "main_agent"
        stats = await store.get_project_trace_agent_stats("project-a")
        assert stats["sources"] == [{"agent": "main_agent", "count": 2}]
        assert stats["total"] == 2

        validation = await store.validate_trace_chain(before)
        assert validation["valid"] is True
        assert validation["chain_trace_ids"] == [before, after]
        assert validation["terminal_trace_id"] == after
        assert {check["key"] for check in validation["checks"]} >= {
            "lineage", "event_sequence", "turn_anchor",
        }

        conn = await database.get_connection()
        await conn.execute("UPDATE traces SET next_trace_id = NULL WHERE trace_id = ?", (before,))
        await conn.commit()
        await conn.close()
        broken = await store.validate_trace_chain(after)
        assert broken["valid"] is False
        assert any(check["key"] == "previous_reciprocal" for check in broken["checks"])

    asyncio.run(scenario())
