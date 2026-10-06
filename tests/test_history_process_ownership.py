"""Run evidence belongs to demands; file units reference it without duplicating text."""

import json
import sqlite3

from novelagent.history import DocumentHistoryStore
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.history.process_repair import repair_process_ownership


def setup_run(tmp_path, run="one"):
    store = DocumentHistoryStore(tmp_path)
    root = store.start_request("root", "session", "修改世界观和卷纲")
    request = store.run_request_id("root", run)
    store.start_request(request, "session", "", parent_request_id="root")
    store.set_requirement_refs(request, root, [f"{root}:input"])
    return store, root, request


def test_multiple_files_share_run_process_and_root_extraction(tmp_path):
    store, root, request = setup_run(tmp_path)
    store.append_request_event(request, "main_delegation", "同步两份文件")
    for path in ("world/a.md", "outlines/a.md"):
        store.append_document_event(request, path, "outliner_thinking", "先确定局部和全局的区别")
        store.append_document_event(request, path, "outliner_output", "两份文件已同步")
        store.append_document_event(request, path, "file_change_attempt", json.dumps({"path": path}))
    store.close_request(request, "completed", "两份文件已同步")
    store.close_request("root", "completed", "完成")
    with sqlite3.connect(store.db_path) as db:
        rows = db.execute("SELECT history_id,kind,content FROM history_events WHERE kind IN "
                          "('main_delegation','outliner_thinking','outliner_output')").fetchall()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {root}
    ids = []
    for path in ("world/a.md", "outlines/a.md"):
        unit = store.get_unit(store._unit_id(request, "document", path))
        process = [e for e in unit["events"] if e["kind"] == "outliner_thinking"]
        assert len(process) == 1 and process[0]["history_id"] == root
        ids.append(process[0]["event_id"])
    assert ids[0] == ids[1]
    pipeline = KnowledgePipeline(None, tmp_path.parent, None, "unused")
    evidence, _ = pipeline._evidence(tmp_path.name, store.get_unit(root))
    assert sum(e["kind"] == "outliner_thinking" for e in evidence) == 1


def test_identical_text_from_different_runs_is_not_deduplicated(tmp_path):
    store, root, first = setup_run(tmp_path)
    second = store.run_request_id("root", "two")
    store.start_request(second, "session", "", parent_request_id="root")
    store.set_requirement_refs(second, root, [f"{root}:input"])
    for request in (first, second):
        store.append_document_event(request, "world/a.md", "outliner_thinking", "检查口径")
    thoughts = [e for e in store.get_unit(root)["events"] if e["kind"] == "outliner_thinking"]
    assert len(thoughts) == 2
    assert {e["scope"]["run_id"] for e in thoughts} == {"one", "two"}


def test_changing_requirement_answers_keeps_process_references(tmp_path):
    store, root, request = setup_run(tmp_path)
    store.append_request_event(request, "main_delegation", "修改卷纲")
    store.append_document_event(request, "outlines/a.md", "outliner_thinking", "原思考")
    answer = store.append_request_event("root", "user_answer", "保留原方案")
    store.set_requirement_refs(request, root, [f"{root}:input", answer])
    unit = store.get_unit(store._unit_id(request, "document", "outlines/a.md"))
    assert any(e["content"] == "原思考" for e in unit["events"])
    assert answer in unit["requirement_event_ids"]


def test_review_process_is_not_moved_into_user_requirement(tmp_path):
    store, root, request = setup_run(tmp_path, "review")
    review = store.start_review(request, "")
    store.record_review(request, "检查常规写作要求", "建议避免重复")
    assert any(e["kind"] == "reviewer_thinking" for e in store.get_unit(review)["events"])
    assert not any(e["kind"] == "reviewer_thinking" for e in store.get_unit(root)["events"])


def test_legacy_repair_uses_source_identity_and_keeps_ids_status_and_versions(tmp_path):
    store, root, request = setup_run(tmp_path)
    files = [store.append_document_event(request, path, "file_change_attempt", path)
             for path in ("world/a.md", "outlines/a.md")]
    with store._connect() as db:
        for hid, eid, source in [(files[0], "copy1", "evt_one"),
                                 (files[1], "copy2", "evt_one"),
                                 (files[0], "second-call", "evt_two")]:
            db.execute("INSERT INTO history_events(event_id,history_id,kind,content,scope_json) VALUES (?,?,?,?,?)",
                       (eid, hid, "outliner_thinking", "相同内容", json.dumps({"source_event_ids": [source], "run_id": "one"})))
        db.execute("UPDATE history_units SET analysis_status='complete'")
        before = db.execute("SELECT history_id,previous_history_id,final_revision_id,analysis_status FROM history_units ORDER BY rowid").fetchall()
    original = store.db_path.read_bytes()
    assert repair_process_ownership(tmp_path)["aliases"] == 1
    assert store.db_path.read_bytes() == original
    result = repair_process_ownership(tmp_path, apply=True)
    assert result["backup"] and result["moved"] == 2 and result["aliases"] == 1
    with store._connect() as db:
        assert db.execute("SELECT history_id,previous_history_id,final_revision_id,analysis_status FROM history_units ORDER BY rowid").fetchall() == before
        assert {r[0] for r in db.execute("SELECT event_id FROM history_events")} >= {"copy1", "copy2", "second-call"}
        assert db.execute("SELECT content FROM history_events WHERE event_id='copy2'").fetchone()[0] == ""
    assert len([e for e in store.get_unit(root)["events"] if e["kind"] == "outliner_thinking"]) == 2
    assert {e["event_id"] for e in store.get_unit(files[1])["events"] if e["kind"] == "outliner_thinking"} == {"copy1", "second-call"}
    assert repair_process_ownership(tmp_path, apply=True) == {
        "mode": "apply", "moved": 0, "aliases": 0, "references": 0, "redirected_links": 0, "backup": ""}


def test_linked_revisions_resolve_shared_evidence_instead_of_copying_it(tmp_path):
    store, root, request = setup_run(tmp_path)
    path = tmp_path / "world/a.md"
    store.record_revision(path, "rev_one", None, "正文", "正文", evidence={
        "history_request_id": request, "run_id": "one", "writer_thinking": "同一思考"})
    store.append_request_event(request, "writer_thinking", "同一思考")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert any(e["content"] == "同一思考" for e in store.get_revision("rev_one")["evidence"])


def test_two_identical_occurrences_in_one_run_keep_their_explicit_ids(tmp_path):
    store, root, request = setup_run(tmp_path)
    for eid in ("turn-one", "turn-two"):
        store.append_request_event(request, "writer_thinking", "检查一致性", event_id=eid)
    assert {e["event_id"] for e in store.get_unit(root)["events"] if e["kind"] == "writer_thinking"} == {"turn-one", "turn-two"}


def test_direct_main_edits_reference_process_before_and_after_file_creation(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    root = store.start_request("root", "session", "直接修改")
    store.set_requirement_refs("root", root, [root + ":input"])
    first = store.append_request_event("root", "main_agent_thinking", "修改前思考")
    file = store.append_document_event("root", "world/a.md", "file_change_attempt", "写入")
    second = store.append_request_event("root", "main_agent_output", "结果")
    assert {e["event_id"] for e in store.get_unit(file)["events"]} >= {first, second}


def test_split_legacy_thinking_and_source_aliases_are_normalized_in_one_pass(tmp_path):
    store, root, request = setup_run(tmp_path)
    files = [store.append_document_event(request, p, "file_change_attempt", p)
             for p in ("world/a.md", "outlines/a.md")]
    with store._connect() as db:
        for index, hid in enumerate(files):
            aggregate = f"old{index}"
            part = aggregate + ":part:evt_thought"
            for eid in (aggregate, part):
                db.execute("INSERT INTO history_events(event_id,history_id,kind,content) VALUES (?,?,?,?)", (eid, hid, "outliner_thinking", "原始思考"))
            db.execute("INSERT INTO history_event_order(event_id,order_status) VALUES (?,'superseded')", (aggregate,))
            db.execute("INSERT INTO history_event_order(event_id,event_order,order_status,original_event_id) VALUES (?,10,'verified',?)", (part, aggregate))
            db.execute("INSERT INTO history_source_events VALUES ('evt_thought',?)", (part,))
    result = repair_process_ownership(tmp_path, apply=True)
    assert result["aliases"] == 3  # Prefer the visible original part to its archive.
    assert repair_process_ownership(tmp_path)["aliases"] == 0
    assert repair_process_ownership(tmp_path)["redirected_links"] == 0
    for hid in files:
        thoughts = [e for e in store.get_unit(hid)["events"] if e["kind"] == "outliner_thinking"]
        assert len(thoughts) == 1 and thoughts[0]["history_id"] == root
    with store._connect() as db:
        assert db.execute("SELECT e.content FROM history_source_events s JOIN history_events e ON e.event_id=s.event_id").fetchall()[0][0] == "原始思考"
        assert db.execute("SELECT COUNT(*) FROM history_source_events").fetchone()[0] == 1
