import hashlib
import json
import sqlite3

from novelagent.history import DocumentHistoryStore
from novelagent.history.backfill import backfill
from novelagent.history.evidence_repair import repair_history_evidence
from novelagent.history.legacy_tools import normalize_tool_events
from novelagent.history.order_repair import _source_evidence, repair_history_order


def _event(eid, kind, payload, **metadata):
    return {"event_id": eid, "event_type": kind, "payload": payload, **metadata}


def _call(cid, tool, params):
    return {"id": cid, "function": {"name": tool, "arguments": json.dumps(params, ensure_ascii=False)}}


def _fixture(tmp_path):
    project = tmp_path / "workspace/project"
    project.mkdir(parents=True)
    trace = tmp_path / "trace.sqlite3"
    task = "删除全书低魔，修改 world/worldview.md 和 outlines/outline_1.0.0.md"
    report = "Agent 报告：world/worldview.md 和 outlines/outline_1.0.0.md 已修改"
    events = [
        _event("user", "user_message", {"content": "仅首个副本低魔"}),
        _event("assistant", "assistant_turn", {"content": "交给 outliner", "tool_calls": [
            _call("checkpoint", "CreateTraceCheckpoint", {"reason": "世界观修正"}),
            _call("outline", "SubAgent", {"preset": "outliner", "task": task})]}),
        _event("checkpoint-result", "tool_result", {"tool": "", "success": True,
                                                     "data": {"queued": True, "reason": "世界观修正"}}),
        _event("outline-result", "tool_result", {"tool": "", "success": True, "data": report}),
    ]
    with sqlite3.connect(trace) as db:
        db.executescript("""
            CREATE TABLE traces (trace_id TEXT,session_id TEXT,project_id TEXT,source_agent TEXT,
              source TEXT,status TEXT,user_message TEXT,final_answer TEXT,started_at TEXT,turn_no INTEGER);
            CREATE TABLE trace_events (event_id TEXT,trace_id TEXT,sequence_no INTEGER,event_type TEXT,
              payload_json TEXT,created_at TEXT);
            INSERT INTO traces VALUES ('trace','session','project','main_agent','live','failed',
              '仅首个副本低魔','', '2026-01-01T00:00:00+00:00',1);
        """)
        for seq, event in enumerate(events, 1):
            db.execute("INSERT INTO trace_events VALUES (?, 'trace', ?, ?, ?, ?)",
                       (event["event_id"], seq, event["event_type"], json.dumps(event["payload"], ensure_ascii=False),
                        f"2026-01-01T00:00:{seq:02}+00:00"))
    store = DocumentHistoryStore(project)
    hid = store.start_request("trace", "session", "仅首个副本低魔")
    store.append_request_event("trace", "main_agent_output", "交给 outliner")
    store.close_request("trace", "failed", "交给 outliner", ["trace"], 1)
    store.set_analysis_status(hid, "complete")
    return trace, project, store, hid, task, report


def test_repair_preserves_ids_versions_and_status_and_is_idempotent(tmp_path):
    trace, project, store, hid, task, output = _fixture(tmp_path)
    original_trace = hashlib.sha256(trace.read_bytes()).hexdigest()
    original_history = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    dry = repair_history_evidence(trace, project)
    assert dry["events_added"] == 2
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == original_history
    result = repair_history_evidence(trace, project, apply=True)
    assert result["histories"] == [hid]
    assert result["backup"]
    assert hashlib.sha256(trace.read_bytes()).hexdigest() == original_trace
    unit = store.get_unit(hid)
    assert unit["analysis_status"] == "complete" and unit["status"] == "failed"
    assert unit.get("revisions", []) == []
    assert {(e["kind"], e["content"]) for e in unit["events"]} >= {
        ("main_delegation", task), ("outliner_output", output)}
    repair_history_order(trace, project, apply=True)
    unit = store.get_unit(hid)
    assert [e["kind"] for e in unit["events"]] == [
        "user_input", "main_agent_output", "main_delegation", "outliner_output"]
    assert all(e["order_status"] == "verified" for e in unit["events"])
    assert repair_history_evidence(trace, project)["events_added"] == 0
    assert repair_history_evidence(trace, project, apply=True)["backup"] == ""
    assert repair_history_order(trace, project)["changed"] == 0
    assert unit["events"][-1]["scope"]["artifact_paths"] == ["outlines/outline_1.0.0.md", "world/worldview.md"]


def test_new_backfill_keeps_embedded_delegation_and_report(tmp_path):
    trace, project, _, _, task, output = _fixture(tmp_path)
    workspace = tmp_path / "new-workspace"
    (workspace / project.name).mkdir(parents=True)
    result = backfill(trace, workspace, apply=True, project_id=project.name)
    assert result["errors"] == []
    store = DocumentHistoryStore(workspace / project.name)
    unit = store.get_unit(store._unit_id("trace", "request"))
    assert {(e["kind"], e["content"]) for e in unit["events"]} >= {
        ("main_delegation", task), ("outliner_output", output)}


def test_ambiguous_unlabelled_results_are_not_assigned_by_position():
    events = [
        _event("a", "assistant_turn", {"tool_calls": [
            _call("one", "SubAgent", {"preset": "outliner", "task": "修改 world/a.md"}),
            _call("two", "SubAgent", {"preset": "reviewer", "task": "检查 world/a.md"})]}),
        _event("r", "tool_result", {"tool": "", "success": True, "data": "world/a.md 完成"}),
    ]
    normalized = normalize_tool_events(events)
    assert normalized[-1]["payload"]["tool"] == ""
    assert "call_params" not in normalized[-1]["payload"]
    assert normalize_tool_events(normalized) == normalized
    candidates = _source_evidence({"events": events})
    assert any(c["kind"] == "unattributed_tool_result" and 'world/a.md 完成' in c["content"] for c in candidates)


def test_call_id_disambiguates_embedded_reports_without_duplicate_call_events():
    calls = [_call("one", "SubAgent", {"preset": "outliner", "task": "讨论方向"}),
             _call("two", "SubAgent", {"preset": "reviewer", "task": "评价方案"})]
    events = [_event("a", "assistant_turn", {"tool_calls": calls}),
              _event("r", "tool_result", {"tool_call_id": "two", "tool": "", "data": "建议修改"})]
    normalized = normalize_tool_events(events)
    assert normalized[-1]["payload"]["call_params"]["preset"] == "reviewer"
    assert normalize_tool_events(normalized) == normalized


def test_explicit_and_embedded_call_aliases_are_not_duplicated():
    call = _call("one", "SubAgent", {"preset": "outliner", "task": "讨论方向"})
    events = [_event("a", "assistant_turn", {"tool_calls": [call]}),
              _event("c", "tool_call", {"tool": "SubAgent", "tool_call_id": "one", "params": {"task": "讨论方向"}})]
    assert normalize_tool_events(events) == events


def test_reversed_tool_results_use_call_identity_not_fifo():
    events = [_event("a", "assistant_turn", {"tool_calls": [
        _call("one", "Read", {"path": "world/a.md"}),
        _call("two", "Read", {"path": "world/b.md"})]}),
        _event("b", "tool_result", {"tool_call_id": "two", "success": True, "data": "B正文"}),
        _event("c", "tool_result", {"tool_call_id": "one", "success": True, "data": "A正文"})]
    candidates = [json.loads(c["content"]) for c in _source_evidence({"events": events}) if c["kind"] == "document_reference"]
    assert candidates == [{"params": {"path": "world/b.md"}, "content": "B正文"},
                          {"params": {"path": "world/a.md"}, "content": "A正文"}]


def test_existing_file_change_with_run_metadata_is_not_duplicated(tmp_path):
    trace, project, store, _, _, _ = _fixture(tmp_path)
    child = store.run_request_id("trace", "writer")
    store.start_request(child, "session", "修改", parent_request_id="trace")
    params = {"path": "chapters/a.md", "old_string": "旧", "new_string": "新"}
    store.append_document_event(child, params["path"], "file_change_attempt", json.dumps({
        "tool": "Edit", "params": params, "success": True, "result": "已修改", "run_id": "writer"}, ensure_ascii=False))
    store.close_request(child, "completed", "", ["trace"], 1)
    with sqlite3.connect(trace) as db:
        for seq, kind, payload in [(5, "subagent_tool_call", {"run_id": "writer", "tool": "Edit", "params": params}),
                                   (6, "subagent_tool_result", {"run_id": "writer", "tool": "Edit", "success": True, "data": "已修改"})]:
            db.execute("INSERT INTO trace_events VALUES (?, 'trace', ?, ?, ?, '2026-01-01')",
                       (str(seq), seq, kind, json.dumps(payload, ensure_ascii=False)))
    repair_history_evidence(trace, project, apply=True)
    unit = store.get_unit(store._unit_id(child, "document", params["path"]))
    assert len([e for e in unit["events"] if e["kind"] == "file_change_attempt"]) == 1


def test_full_task_and_short_summary_at_same_source_do_not_collide(tmp_path):
    trace, project, _, _, _, _ = _fixture(tmp_path)
    with sqlite3.connect(trace) as db:
        db.execute("INSERT INTO trace_events VALUES ('start', 'trace', 5, 'subagent_subagent_start', ?, '2026-01-01')",
                   (json.dumps({"task_prompt": "完整的讨论任务", "task_summary": "讨论任务"}, ensure_ascii=False),))
    repair_history_evidence(trace, project, apply=True)
    assert repair_history_evidence(trace, project)["events_added"] == 0


def test_task_attachments_keep_original_snapshot_without_inventing_revision(tmp_path):
    trace, project, store, hid, _, _ = _fixture(tmp_path)
    snapshot = "[当前修订: rev_old]\n旧设定：全书低魔"
    with sqlite3.connect(trace) as db:
        payload = json.loads(db.execute("SELECT payload_json FROM trace_events WHERE event_id='assistant'").fetchone()[0])
        args = json.loads(payload["tool_calls"][1]["function"]["arguments"])
        args["attachments"] = [{"path": "world/worldview.md", "content": snapshot}]
        payload["tool_calls"][1]["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
        db.execute("UPDATE trace_events SET payload_json=? WHERE event_id='assistant'", (json.dumps(payload, ensure_ascii=False),))
    repair_history_evidence(trace, project, apply=True)
    repair_history_order(trace, project, apply=True)
    unit = store.get_unit(hid)
    references = [e for e in unit["events"] if e["kind"] == "document_reference"]
    assert json.loads(references[0]["content"])["content"] == snapshot
    assert references[0]["order_status"] == "verified"
    assert store.get_revision("rev_old") is None
    assert repair_history_evidence(trace, project)["events_added"] == 0


def test_identical_embedded_delegations_remain_two_ordered_occurrences(tmp_path):
    trace, project, store, hid, task, _ = _fixture(tmp_path)
    with sqlite3.connect(trace) as db:
        payload = {"content": "交给 outliner", "tool_calls": [
            _call(cid, "SubAgent", {"preset": "outliner", "task": task}) for cid in ("one", "two")]}
        db.execute("UPDATE trace_events SET payload_json=? WHERE event_id='assistant'", (json.dumps(payload, ensure_ascii=False),))
    repair_history_evidence(trace, project, apply=True)
    repair_history_order(trace, project, apply=True)
    delegations = [e for e in store.get_unit(hid)["events"] if e["kind"] == "main_delegation"]
    assert len(delegations) == 2
    assert len({e["event_order"] for e in delegations}) == 2
    assert repair_history_order(trace, project)["changed"] == 0
