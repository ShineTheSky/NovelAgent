import hashlib
import json
import sqlite3

from novelagent.history import DocumentHistoryStore
from novelagent.history.backfill import backfill, rebuild_project_history, reorganize
from novelagent.versioning import revision_manager


def _trace_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE traces (
            trace_id TEXT, session_id TEXT, project_id TEXT, source_agent TEXT,
            source TEXT, status TEXT, user_message TEXT, final_answer TEXT,
            started_at TEXT, previous_trace_id TEXT, turn_no INTEGER
        );
        CREATE TABLE trace_events (
            event_id TEXT, trace_id TEXT, sequence_no INTEGER,
            event_type TEXT, payload_json TEXT
        );
    """)
    return db


def _event(db, trace_id, sequence, kind, payload):
    db.execute("INSERT INTO trace_events VALUES (?, ?, ?, ?, ?)",
               (f"event-{trace_id}-{sequence}", trace_id, sequence, kind,
                json.dumps(payload, ensure_ascii=False)))


def test_live_trace_backfills_file_evidence_and_only_verified_current_revision(tmp_path):
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    path = project / "chapters" / "content_1.1.1.md"
    path.parent.mkdir(parents=True)
    body = "修订后的正文"
    metadata = {"revision_id": "rev_current", "parent_revision_id": "rev_old",
                "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
    path.write_text(revision_manager.render(metadata, body), encoding="utf-8")
    trace_db = tmp_path / "trace.db"
    with _trace_db(trace_db) as db:
        db.execute("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("trace-a", "session-a", "project-a", "main_agent", "live", "completed",
                    "修改正文", "完成", "2026-01-01", None, 1))
        _event(db, "trace-a", 1, "user_message", {"content": "修改正文"})
        _event(db, "trace-a", 2, "user_answer", {"answers": [{"selected_options": ["克制"]}]})
        _event(db, "trace-a", 3, "tool_call", {"tool": "SubAgent", "params": {
            "preset": "chapter_polisher", "task": "修改 chapters/content_1.1.1.md"}})
        _event(db, "trace-a", 4, "subagent_subagent_start", {
            "run_id": "run-1", "preset": "chapter_polisher",
            "task_prompt": "修改 chapters/content_1.1.1.md"})
        _event(db, "trace-a", 5, "subagent_thinking", {"run_id": "run-1", "content": "需要压低情绪"})
        _event(db, "trace-a", 6, "subagent_tool_call", {
            "run_id": "run-1", "tool": "Edit", "params": {
                "path": "chapters/content_1.1.1.md", "old_string": "旧句", "new_string": body,
                "expected_revision_id": "rev_old"}})
        _event(db, "trace-a", 7, "subagent_tool_result", {
            "run_id": "run-1", "tool": "Edit", "success": True,
            "data": "当前修订: rev_current"})
        _event(db, "trace-a", 8, "subagent_subagent_done", {
            "run_id": "run-1", "preset": "chapter_polisher", "result": "已润色",
            "revision_events": [{"path": "chapters/content_1.1.1.md",
                                 "revision_id": "rev_current", "parent_revision_id": "rev_old"}]})

    dry = backfill(trace_db, workspace)
    assert dry["document_units"] == 1
    assert dry["verified_current_revisions"] == 1
    assert not (project / ".history").exists()

    result = backfill(trace_db, workspace, apply=True)
    assert result["imported"] == 1
    store = DocumentHistoryStore(project)
    units = store.list_document_units("chapters/content_1.1.1.md")
    assert len(units) == 1
    unit = units[0]
    assert unit["analysis_status"] == "deferred"
    assert unit["final_revision_id"] == "rev_current"
    assert unit["trace_ids"] == ["trace-a"]
    assert store.latest("chapters/content_1.1.1.md")["body_md"] == body
    kinds = {event["kind"] for event in unit["events"]}
    assert {"user_input", "user_answer", "main_delegation", "file_change_attempt",
            "revision_reference", "chapter_polisher_thinking", "chapter_polisher_output"} <= kinds
    assert backfill(trace_db, workspace, apply=True)["existing"] == 1
    assert len(store.list_document_units("chapters/content_1.1.1.md")) == 1
    check = backfill(trace_db, workspace, check=True)
    assert check["checked"] == 1
    assert check["errors"] == []


def test_historical_turns_become_separate_conversation_units(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "project-a").mkdir(parents=True)
    trace_db = tmp_path / "trace.db"
    with _trace_db(trace_db) as db:
        db.execute("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("trace-old", "session-a", "project-a", "main_agent", "historical",
                    "completed", "第二个问题", "第二个回答", "2025-01-01", None, None))
        for no in (1, 2):
            _event(db, "trace-old", no * 2 - 1, "user_message",
                   {"turn_no": no, "content": f"问题{no}"})
            _event(db, "trace-old", no * 2, "assistant_turn",
                   {"turn_no": no, "content": f"回答{no}"})
    report = backfill(trace_db, workspace, apply=True)
    assert report["imported"] == 2
    store = DocumentHistoryStore(workspace / "project-a")
    for no in (1, 2):
        unit = store.get_unit(store._unit_id(f"trace-old:turn:{no}", "request"))
        assert unit["kind"] == "conversation"
        assert unit["session_turn_no"] == no
        assert unit["assistant_response"] == f"回答{no}"
        assert unit["events"][0]["content"] == f"问题{no}"
        assert any(e["kind"] == "main_agent_output" and e["content"] == f"回答{no}"
                   for e in unit["events"])


def test_conversation_keeps_agent_reasoning_and_repairs_missing_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    project.mkdir(parents=True)
    trace_db = tmp_path / "trace.db"
    with _trace_db(trace_db) as db:
        db.execute("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("trace-chat", "session-a", "project-a", "main_agent", "live",
                    "completed", "讨论设定", "好的", "2026-01-01", None, 1))
        _event(db, "trace-chat", 1, "user_message", {"content": "讨论设定"})
        _event(db, "trace-chat", 2, "assistant_turn", {
            "content": "考虑双时间线", "reasoning_content": "先比较两种结构"})
        _event(db, "trace-chat", 3, "subagent_subagent_start", {
            "run_id": "brainstorm", "preset": "outliner", "task_prompt": "讨论时间线"})
        _event(db, "trace-chat", 4, "subagent_thinking", {
            "run_id": "brainstorm", "content": "两条线交织"})
        _event(db, "trace-chat", 5, "subagent_subagent_done", {
            "run_id": "brainstorm", "preset": "outliner", "result": "建议交替叙事"})
    assert backfill(trace_db, workspace, apply=True)["imported"] == 1
    store = DocumentHistoryStore(project)
    unit = store.get_unit(store._unit_id("trace-chat", "request"))
    assert unit["kind"] == "conversation"
    assert {"main_agent_thinking", "main_agent_output", "outliner_thinking",
            "outliner_output", "subagent_task"} <= {e["kind"] for e in unit["events"]}

    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM history_events WHERE history_id = ? AND kind = 'outliner_thinking'",
                   (unit["history_id"],))
    assert backfill(trace_db, workspace, check=True)["errors"]
    repaired = backfill(trace_db, workspace, repair=True)
    assert repaired["repaired_events"] == 1
    assert backfill(trace_db, workspace, check=True)["errors"] == []
    assert backfill(trace_db, workspace, repair=True)["repaired_events"] == 0


def test_reorganize_splits_multiple_agent_runs_on_one_file(tmp_path):
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    path = project / "chapters" / "content_1.1.1.md"
    path.parent.mkdir(parents=True)
    body = "最终稿"
    path.write_text(revision_manager.render({
        "revision_id": "rev_4", "parent_revision_id": "rev_3",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }, body), encoding="utf-8")
    trace_db = tmp_path / "trace.db"
    with _trace_db(trace_db) as db:
        db.execute("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("trace-a", "session-a", "project-a", "main_agent", "live", "completed",
                    "修改正文", "完成", "2026-01-01", None, 1))
        _event(db, "trace-a", 1, "user_message", {"content": "修改正文"})
        sequence = 2
        for run_id, refs in (("run-1", [("rev_1", "rev_0"), ("rev_2", "rev_1")]),
                             ("run-2", [("rev_3", "rev_2"), ("rev_4", "rev_3")])):
            _event(db, "trace-a", sequence, "subagent_subagent_start", {
                "run_id": run_id, "preset": "chapter_polisher",
                "task_prompt": "修改 chapters/content_1.1.1.md"})
            sequence += 1
            for revision_id, parent_revision_id in refs:
                _event(db, "trace-a", sequence, "subagent_tool_call", {
                    "run_id": run_id, "tool": "Edit", "params": {
                        "path": "chapters/content_1.1.1.md", "old_string": "旧", "new_string": "新"}})
                sequence += 1
                _event(db, "trace-a", sequence, "subagent_tool_result", {
                    "run_id": run_id, "tool": "Edit", "success": True,
                    "data": f"当前修订: {revision_id}"})
                sequence += 1
            _event(db, "trace-a", sequence, "subagent_subagent_done", {
                "run_id": run_id, "preset": "chapter_polisher", "result": "已修改",
                "revision_events": [
                    {"path": "chapters/content_1.1.1.md", "revision_id": revision_id,
                     "parent_revision_id": parent_revision_id}
                    for revision_id, parent_revision_id in refs],
            })
            sequence += 1
    assert backfill(trace_db, workspace, apply=True)["imported"] == 1
    store = DocumentHistoryStore(project)
    assert len(store.list_document_units("chapters/content_1.1.1.md")) == 1
    report = reorganize(trace_db, workspace, apply=True)
    assert report["migrated"] == 2
    assert report["removed_legacy_aggregates"] == 1
    assert not store.db_path.with_suffix(".pre-run-history.bak").exists()
    assert store.get_unit(store._unit_id("trace-a", "document", "chapters/content_1.1.1.md")) is None
    assert backfill(trace_db, workspace, check=True)["errors"] == []
    units = store.list_document_units("chapters/content_1.1.1.md")
    assert len(units) == 2
    assert [(unit["base_revision_id"], unit["final_revision_id"]) for unit in units] == [
        ("rev_0", "rev_2"), ("rev_2", "rev_4")]
    assert [len([event for event in unit["events"] if event["kind"] == "revision"])
            for unit in units] == [2, 2]
    assert units[0]["revisions"] == [None, None]
    assert units[1]["revisions"][-1]["body_md"] == body
    assert reorganize(trace_db, workspace, apply=True)["existing"] == 2
    assert len(store.list_document_units("chapters/content_1.1.1.md")) == 2


def test_linked_traces_split_by_user_turn_but_keep_compression_in_same_turn(tmp_path):
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    path = project / "chapters" / "content_1.1.2.md"
    path.parent.mkdir(parents=True)
    body = "第三稿"
    path.write_text(revision_manager.render({
        "revision_id": "rev_3", "parent_revision_id": "rev_2",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }, body), encoding="utf-8")
    trace_db = tmp_path / "trace.db"
    with _trace_db(trace_db) as db:
        for trace_id, previous, turn, user, started in (
            ("trace-1", None, 1, "第一轮：修改开头", "2026-01-01"),
            ("trace-2", "trace-1", 2, "第二轮：修改结尾", "2026-01-02"),
            ("trace-3", "trace-2", 2, "[context_compression] 第二轮：修改结尾", "2026-01-03"),
        ):
            db.execute("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (trace_id, "session-a", "project-a", "main_agent", "live", "completed",
                        user, "完成", started, previous, turn))
        _event(db, "trace-1", 1, "user_message", {"content": "第一轮：修改开头"})
        _event(db, "trace-1", 2, "subagent_subagent_start", {
            "run_id": "run-1", "preset": "chapter_polisher",
            "task_prompt": "修改 chapters/content_1.1.2.md 的开头"})
        _event(db, "trace-1", 3, "subagent_subagent_done", {
            "run_id": "run-1", "preset": "chapter_polisher", "result": "已改开头",
            "revision_events": [{"path": "chapters/content_1.1.2.md",
                                 "revision_id": "rev_1", "parent_revision_id": "rev_0"}]})
        _event(db, "trace-2", 1, "user_message", {"content": "第二轮：修改结尾"})
        _event(db, "trace-3", 1, "subagent_subagent_start", {
            "run_id": "run-2", "preset": "chapter_polisher",
            "task_prompt": "修改 chapters/content_1.1.2.md 的结尾"})
        _event(db, "trace-3", 2, "subagent_subagent_done", {
            "run_id": "run-2", "preset": "chapter_polisher", "result": "已改结尾",
            "revision_events": [{"path": "chapters/content_1.1.2.md",
                                 "revision_id": "rev_3", "parent_revision_id": "rev_1"}]})

    assert backfill(trace_db, workspace, apply=True)["imported"] == 2
    assert reorganize(trace_db, workspace, apply=True)["migrated"] == 2
    units = DocumentHistoryStore(project).list_document_units("chapters/content_1.1.2.md")
    assert len(units) == 2
    assert [unit["session_turn_no"] for unit in units] == [1, 2]
    assert [unit["trace_ids"] for unit in units] == [["trace-1"], ["trace-3"]]
    assert [unit["events"][0]["content"] for unit in units] == [
        "第一轮：修改开头", "第二轮：修改结尾"]
    assert [unit["base_revision_id"] for unit in units] == ["rev_0", "rev_1"]
    assert [unit["final_revision_id"] for unit in units] == ["rev_1", "rev_3"]

    store = DocumentHistoryStore(project)
    store.start_request("wrong-root", "session-a", "错误的聚合输入")
    store.append_document_event("wrong-root", "chapters/content_1.1.2.md",
                                "file_change_attempt", "错误记录")
    store.close_request("wrong-root", "completed", "错误", ["trace-1"], 2)
    wrong_id = store._unit_id("wrong-root", "document", "chapters/content_1.1.2.md")
    store.set_analysis_status(wrong_id, "deferred")
    store.set_analysis_status(store._unit_id("wrong-root", "request"), "routed")
    assert rebuild_project_history(trace_db, workspace, "project-a")["old_units"]["document"] == 3
    rebuilt = rebuild_project_history(trace_db, workspace, "project-a", apply=True)
    assert rebuilt["errors"] == []
    assert store.get_unit(wrong_id) is None
    assert not store.db_path.with_suffix(".pre-rebuild.bak").exists()
    assert [unit["events"][0]["content"] for unit in store.list_document_units(
        "chapters/content_1.1.2.md")] == ["第一轮：修改开头", "第二轮：修改结尾"]
