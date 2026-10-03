from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest
import yaml

from novelagent.history import DocumentHistoryStore
from novelagent.history.memory_links import link_records
from novelagent.history.source_repair import repair_sources


@pytest.fixture
def repair_data(tmp_path):
    project = tmp_path / "project"
    store = DocumentHistoryStore(project)
    root = store.start_request("trace", "session", "修改身份")
    store.append_document_event("trace", "chapters/a.md", "writer_thinking", "写正文")
    task = "只改这一段，±20 字以内"
    task_id = store.append_request_event("trace", "main_delegation", task)
    store.close_request("trace", "completed", "完成", ["trace"], 1)
    source = tmp_path / "trace.sqlite3"
    report = "审阅结论（reviewer）：进退机制冲突；已修订。"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE traces(trace_id TEXT,session_id TEXT,project_id TEXT,source_agent TEXT,
                source TEXT,status TEXT,user_message TEXT,final_answer TEXT,started_at TEXT,turn_no INTEGER);
            CREATE TABLE trace_events(event_id TEXT,trace_id TEXT,sequence_no INTEGER,event_type TEXT,
                payload_json TEXT,created_at TEXT);
        """)
        db.execute("INSERT INTO traces VALUES ('trace','session','project','main_agent','live','completed','修改身份','完成','2026-01-01',1)")
        for eid, seq, kind, payload in (
            ("user", 1, "user_message", {"content": "修改身份"}),
            ("task", 2, "tool_call", {"tool": "SubAgent", "params": {"preset": "chapter_polisher", "task": task}}),
            ("review", 3, "assistant_turn", {"content": report}),
        ):
            db.execute("INSERT INTO trace_events VALUES (?,'trace',?,?,?,'2026-01-01T00:00:00Z')", (eid, seq, kind, json.dumps(payload, ensure_ascii=False)))
    for rid, title in (("ins_review", "统一机制"), ("mem_task", "任务约束")):
        path = project / (".insight/project" if rid.startswith("ins") else ".memory/reference") / f"{rid}.md"
        path.parent.mkdir(parents=True)
        record = {"id": rid, "title": title, "claim": title, "weight": 30, "support_count": 2,
            "created": "保持", "updated": "不变", "source_event_ids": ["user"], "history_ids": [root],
            "history_source_links": [{"source_event_id": "user", "history_id": root,
                "history_event_ids": [f"{root}:input"], "match": "evidence"}]}
        if rid.startswith("ins"):
            record["source_event_ids"].append("review")
        path.write_text("---\n" + yaml.safe_dump(record, allow_unicode=True) + "---\n\n## 用户要求\n既有内容。\n", encoding="utf-8")
    plan = {"project_id": "project", "review_nodes": [{"source_event_id": "review", "requirement_history_id": root,
        "path": "chapters/a.md", "anchor": "审阅结论", "evidence_origin": "review_retelling"}], "records": [
        {"id": "ins_review", "expected_title": "统一机制", "review_source_event_id": "review",
         "evidence_anchor": "进退机制冲突", "context_source_event_ids": ["user"], "note": "仅有转述"},
        {"id": "mem_task", "expected_title": "任务约束", "add_evidence": [{"source_event_id": "task",
            "history_event_id": task_id, "source_kind": "main_agent_task", "anchor": "±20"}],
         "body_source_note": "字数限制来自主 Agent，不是新增用户偏好。", "note": "已区分来源"},
    ]}
    return project, source, store, root, plan


def _record(project, rid):
    path = next(project.glob(f".*/*/{rid}.md"))
    _, header, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(header), body


def test_dry_run_is_read_only_and_apply_is_backed_up_and_idempotent(repair_data):
    project, source, store, root, plan = repair_data
    fingerprint = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    trace_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    dry = repair_sources(source, project, plan)
    assert dry["changed_records"] == ["ins_review", "mem_task"]
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == fingerprint
    assert not (project / ".history/migration_backups").exists()
    result = repair_sources(source, project, plan, apply=True)
    assert result["backup"]
    backup = project / ".history/migration_backups"
    assert len(list(backup.glob("*/history.sqlite3"))) == 1
    review_id = result["review_nodes"][0]["history_id"]
    unit = store.get_unit(review_id)
    assert unit["kind"] == "review" and unit["analysis_status"] == "complete"
    assert unit["requirement_history_id"] == root
    assert unit["base_revision_id"] == unit["final_revision_id"] == ""
    assert unit["events"][0]["kind"] == "review_retelling"
    assert not unit["events"][0]["scope"]["original_report_available"]
    record, body = _record(project, "ins_review")
    assert record["history_ids"] == [review_id] and root in record["related_history_ids"]
    assert record["semantic_evidence"][0]["evidence_status"] == "retelling_only"
    assert record["weight"] == 30 and record["support_count"] == 2 and record["updated"] == "不变"
    assert body == "\n\n## 用户要求\n既有内容。\n"
    memory, body = _record(project, "mem_task")
    assert "## 用户要求与主 Agent 执行约束" in body
    assert any(e["source_kind"] == "main_agent_task" for e in memory["semantic_evidence"])
    assert memory["weight"] == 30 and memory["support_count"] == 2
    again = repair_sources(source, project, plan, apply=True)
    assert again["changed_records"] == [] and again["backup"] is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == trace_hash
    linked = link_records(source, project, layers=("insight", "memory"), apply=True)
    assert linked["linked"] == 2
    assert _record(project, "ins_review")[0]["history_ids"] == [review_id]
    assert _record(project, "ins_review")[0]["history_source_decisions"]["user"]["match"] == "context"


@pytest.mark.parametrize("failure", ["wrong_project", "missing_source", "wrong_title", "wrong_anchor", "wrong_task"])
def test_invalid_plan_fails_before_writes(repair_data, failure):
    project, source, store, _, original = repair_data
    plan = deepcopy(original)
    if failure == "wrong_project":
        plan["project_id"] = "other"
    elif failure == "missing_source":
        plan["review_nodes"][0]["source_event_id"] = "missing"
    elif failure == "wrong_title":
        plan["records"][0]["expected_title"] = "changed"
    elif failure == "wrong_anchor":
        plan["records"][0]["evidence_anchor"] = "不存在的结论"
    elif failure == "wrong_task":
        plan["records"][1]["add_evidence"][0]["history_event_id"] = f"{store._unit_id('trace','request')}:input"
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        repair_sources(source, project, plan, apply=True)
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    assert not (project / ".history/migration_backups").exists()
