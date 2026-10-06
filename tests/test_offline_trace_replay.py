import json
import sqlite3

import pytest

from novelagent.history.store import DocumentHistoryStore
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.offline_migration import source_manifest
from novelagent.memory.offline_trace_replay import (
    ReplayRejected, replay_completed_retain_trace, _package_source_ids, _remap_package_sources,
)
from scripts.replay_history_retain_trace import main


def replay_fixture(tmp_path, *, trace_status="completed", source_id=None, request_source_id=None):
    project = tmp_path / "project"
    history = DocumentHistoryStore(project)
    history_id = history.start_request("request-1", "session-1", "保持自然衔接")
    history.close_request("request-1", "completed", "已完成")
    event_id = history_id + ":input"
    package_source = source_id or event_id
    requested_source = request_source_id or package_source
    response = {"memories": [{
        "title": "段落衔接要求", "direction": "保持自然衔接", "background": "用户提出正文要求",
        "requirement": "保持自然衔接", "source_text": "保持自然衔接",
        "facts": [{"domain": "writing", "claim": "用户要求保持自然衔接",
                   "source_event_ids": [package_source], "modality": "asserted"}],
    }]}
    trace = tmp_path / "trace.sqlite3"
    with sqlite3.connect(trace) as db:
        db.executescript("""
            CREATE TABLE traces (trace_id TEXT PRIMARY KEY, project_id TEXT, status TEXT, final_answer TEXT);
            CREATE TABLE trace_events (event_id TEXT, trace_id TEXT, sequence_no INTEGER,
                event_type TEXT, actor TEXT, payload_json TEXT);
        """)
        db.execute("INSERT INTO traces VALUES ('retain-1',?,?,?)",
                   (project.name, trace_status, json.dumps(response, ensure_ascii=False)))
        prompt = f"History={history_id}; source_event_id={requested_source}; input=保持自然衔接"
        split = len(prompt) // 2
        events = [
            ("started", 1, "agent_started", "knowledge:retain", {"actor": "knowledge:retain"}),
            ("request", 2, "llm_request", "knowledge:retain", {"message_count": 1, "position": "memory_summary_fallback"}),
            ("message-a", 3, "agent_request_message", "system", {"message_index": 0, "chunk_index": 0,
                "chunk_count": 2, "role": "user", "content": prompt[:split]}),
            ("message-b", 4, "agent_request_message", "system", {"message_index": 0, "chunk_index": 1,
                "chunk_count": 2, "role": "user", "content": prompt[split:]}),
        ]
        db.executemany("INSERT INTO trace_events VALUES (?,?,?,?,?,?)", [
            (eid, "retain-1", seq, event_type, actor, json.dumps(payload, ensure_ascii=False))
            for eid, seq, event_type, actor, payload in events])
    return project, trace, history_id, event_id


def test_fragment_sources_are_mapped_even_when_not_cited_by_facts():
    raw = {"facts": [{"source_event_ids": ["user"]}],
           "fragments": [{"source_event_id": "edit", "before": "旧正文", "after": "新正文"}]}
    assert _package_source_ids(raw) == ["user", "edit"]
    mapped = _remap_package_sources(raw, {"user": "history:user", "edit": "history:edit"})
    assert mapped["fragments"][0]["source_event_id"] == "history:edit"
    assert raw["fragments"][0]["source_event_id"] == "edit"


def test_new_fragment_selection_replays_with_program_filled_versions(tmp_path):
    project, trace, history_id, event_id = replay_fixture(tmp_path)
    history = DocumentHistoryStore(project)
    history.set_requirement_refs("request-1", history_id, [event_id])
    path = project / "world" / "a.md"
    history.record_revision(path, "v1", None, "旧正文", "旧正文")
    history.record_revision(path, "v2", "v1", "新正文", "新正文")
    history.append_document_event("request-1", "world/a.md", "file_change_attempt", json.dumps({
        "tool": "Edit", "success": True, "params": {"path": "world/a.md", "expected_revision_id": "v1",
            "old_string": "旧正文", "new_string": "新正文"}, "result": "当前修订: v2"}), event_id="edit")
    history.attach_revision("request-1", "world/a.md", "v2", "v1")
    history.close_request("request-1", "completed", "已完成")
    with sqlite3.connect(trace) as db:
        response = json.loads(db.execute("SELECT final_answer FROM traces").fetchone()[0])
        response["memories"][0].update(artifact_paths=["world/a.md"], fragments=[
            {"source_event_id": "edit", "before": "旧正文", "after": "新正文"}])
        db.execute("UPDATE traces SET final_answer=?", (json.dumps(response),))
        row = db.execute("SELECT payload_json FROM trace_events WHERE event_id='message-b'").fetchone()
        payload = json.loads(row[0])
        payload["content"] += "; source_event_id=edit"
        db.execute("UPDATE trace_events SET payload_json=? WHERE event_id='message-b'", (json.dumps(payload),))
    before = source_manifest(project)
    report = replay_completed_retain_trace(project, trace, "retain-1")
    assert report["status"] == "replayed_in_temporary_snapshot"
    assert report["packages"][0]["source_event_ids"] == sorted([event_id, "edit"])
    assert report["packages"][0]["readback_verified"]
    assert source_manifest(project) == before


def test_completed_retain_replays_into_snapshot_and_keeps_sources_unchanged(tmp_path):
    project, trace, history_id, event_id = replay_fixture(tmp_path)
    before = source_manifest(project)
    report = replay_completed_retain_trace(project, trace, "retain-1")
    assert report["status"] == "replayed_in_temporary_snapshot"
    assert report["history_id"] == history_id
    assert report["packages"][0]["source_event_ids"] == [event_id]
    assert report["packages"][0]["readback_verified"] is True
    assert report["snapshot_verified"] and report["source_hashes_unchanged"]
    assert report["temporary_snapshot_removed"]
    assert report["application_llm_calls"] == report["embedding_calls"] == 0
    assert report["insight_saved"] is False
    assert source_manifest(project) == before
    serialized = json.dumps(report, ensure_ascii=False)
    assert "保持自然衔接" not in serialized


def test_replay_rejects_source_event_not_in_the_mapped_history(tmp_path):
    project, trace, _, _ = replay_fixture(
        tmp_path, source_id="wrong-history-event", request_source_id="wrong-history-event")
    before = source_manifest(project)
    with pytest.raises(ReplayRejected, match="history_source_missing_or_ambiguous"):
        replay_completed_retain_trace(project, trace, "retain-1")
    assert source_manifest(project) == before


def test_replay_rejects_failed_trace(tmp_path):
    project, trace, _, _ = replay_fixture(tmp_path, trace_status="failed")
    with pytest.raises(ReplayRejected, match="trace_not_completed"):
        replay_completed_retain_trace(project, trace, "retain-1")


def test_cli_writes_machine_readable_report_without_overwriting(tmp_path, capsys):
    project, trace, _, _ = replay_fixture(tmp_path)
    report_path = tmp_path / "report.json"
    result = main(["--project", str(project), "--trace-database", str(trace),
                   "--trace-id", "retain-1", "--output", str(report_path)])
    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "replayed_in_temporary_snapshot"
    assert capsys.readouterr().out == ""
    assert main(["--project", str(project), "--trace-database", str(trace),
                 "--trace-id", "retain-1", "--output", str(report_path)]) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "report_file_exists"


def test_cli_defaults_to_stdout_and_refuses_report_inside_source(tmp_path, capsys):
    project, trace, _, _ = replay_fixture(tmp_path)
    args = ["--project", str(project), "--trace-database", str(trace), "--trace-id", "retain-1"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "replayed_in_temporary_snapshot"
    forbidden = project / "migration-report.json"
    assert main([*args, "--output", str(forbidden)]) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "report_path_overlaps_source"
    assert not forbidden.exists()
