import asyncio

from novelagent.storage import database
from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.trace.recorder import TraceRecorder
from novelagent.trace.store import TraceStore


def test_support_is_counted_once_per_trace(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    item = store.write("insight", {
        "title": "动作场景写清距离", "claim": "动作场景写清距离",
        "content": "动作场景需要明确人物距离。", "category": "project",
        "domain": "writing", "trace_ids": ["trace-a"],
        "source_event_ids": ["event-a"], "weight": 15, "support_count": 1,
    })

    repeated, changed = store.add_support(item, ["trace-a"], ["event-a"], 15)
    added, added_changed = store.add_support(repeated, ["trace-b"], ["event-b"], 15)

    assert changed is False
    assert repeated["support_count"] == 1
    assert repeated["weight"] == 15
    assert added_changed is True
    assert added["support_count"] == 2
    assert added["weight"] == 30
    assert added["support_sources"] == ["trace:trace-a", "trace:trace-b"]


def test_conflict_demotes_memory_and_dependent_pattern(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    memory = store.write("memory", {
        "title": "保持短句", "claim": "动作场景保持短句", "content": "动作场景保持短句。",
        "category": "project", "domain": "writing", "weight": 220,
        "support_count": 3, "trace_ids": ["trace-a"], "source_event_ids": ["event-a"],
    })
    pattern = store.promote_memory_to_pattern(memory, ["trace-a"])

    demoted = store.downgrade_conflict("memory", memory["id"], "candidate-b", "同一场景要求改用长句。")

    assert demoted["layer"] == "insight"
    assert demoted["promotion_status"] == "conflict_review"
    assert not store.can_auto_promote(demoted)
    assert store.get("memory", memory["id"]) is None
    assert store.get("pattern", pattern["id"]) is None
    pattern_insight = next(item for item in store.list("insight") if item.get("origin_pattern_id") == pattern["id"])
    assert pattern_insight["promotion_status"] == "conflict_review"


def test_prepared_data_marks_missing_sources_unverified():
    prepared = FileTraceAnalyzer._stabilize_prepared_data({
        "records": [{"layer": "memory", "source_event_ids": ["missing-event"]}],
    }, "window-a", ["real-event"])

    assert prepared["records"][0]["source_status"] == "unverified"


def test_prepared_window_replays_without_llm_and_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "recovery.db"))

    async def scenario():
        await database.init()
        trace_store = TraceStore()
        recorder = TraceRecorder(trace_store)
        trace_id = await recorder.start("session-a", "project-a", "以后动作场景写清距离。")
        events = await trace_store.list_events(trace_id)
        await recorder.finish(trace_id, "completed")
        await trace_store.append_session_turn(
            "session-a", "以后动作场景写清距离。", "明白。", source_trace_id=trace_id,
        )
        window = await trace_store.capture_pending_trace_window(
            "session-a", "project-a", [{"role": "user", "content": "完整上下文"}],
        )
        event = next(item for item in events if item["event_type"] == "user_message")
        analyzer = FileTraceAnalyzer(None, str(tmp_path / "workspace"), trace_store)
        data = analyzer._stabilize_prepared_data({
            "window_summary": "本轮确认动作场景空间关系要求。",
            "items": [],
            "records": [{
                "layer": "memory", "category": "project", "domain": "writing",
                "title": "动作场景写清距离", "claim": "动作场景写清距离",
                "content": "动作场景需要明确人物距离。", "signal": "strong",
                "source_event_ids": [event["event_id"]], "relation": "new",
            }],
        }, window["window_id"], [event["event_id"]])
        await trace_store.save_trace_window_prepared(window["window_id"], {
            "data": data, "events": events, "source_trace_ids": [trace_id],
            "allowed_source_event_ids": [event["event_id"]], "trace_id": trace_id,
        })

        await analyzer.analyze_window(window["window_id"], "project-a")
        saved = FileLifecycleStore(str(tmp_path / "workspace"), "project-a").list("memory")
        assert len(saved) == 1
        await trace_store.set_trace_window_status(window["window_id"], "prepared")
        await analyzer.analyze_window(window["window_id"], "project-a")
        repeated = FileLifecycleStore(str(tmp_path / "workspace"), "project-a").list("memory")
        assert len(repeated) == 1
        final_window = await trace_store.get_trace_window(window["window_id"])
        assert final_window["status"] == "complete"
        assert final_window["summary"] == "本轮确认动作场景空间关系要求。"

    asyncio.run(scenario())
