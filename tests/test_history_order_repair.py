import hashlib
import json
import sqlite3

from novelagent.history import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.history.memory_links import migrate_requirements
from novelagent.history.order_repair import _source_evidence, repair_history_order


def _fixture(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    trace = tmp_path / "trace.sqlite3"
    path = "chapters/a.md"
    questions = [{"question": "怎么修改？", "options": [{"label": "克制", "description": "用动作表达"}]}]
    changes = [{"path": path, "old_string": old, "new_string": new} for old, new in (("a", "b"), ("b", "c"))]
    sources = [
        ("user_message", {"content": "修改正文"}),
        ("assistant_turn", {"reasoning_content": "先确认要求", "content": "请确认"}),
        ("tool_call", {"tool": "AskUserQuestion", "params": {"questions": questions}}),
        ("question_ask", {"questions": questions}),
        ("user_answer", {"answers": ["克制"]}),
        ("tool_result", {"tool": "AskUserQuestion", "success": True, "data": ["克制"]}),
        ("assistant_turn", {"reasoning_content": "已理解回答"}),
        ("tool_call", {"tool": "SubAgent", "params": {"task": "修改章节"}}),
        ("subagent_subagent_start", {"run_id": "run", "preset": "chapter_polisher", "task_prompt": "修改章节"}),
        ("subagent_thinking", {"run_id": "run", "content": "第一次思考"}),
        ("subagent_tool_call", {"run_id": "run", "tool": "Edit", "params": changes[0]}),
        ("subagent_tool_result", {"run_id": "run", "tool": "Edit", "success": True, "data": "第一次写入"}),
        ("subagent_thinking", {"run_id": "run", "content": "第二次思考"}),
        ("subagent_tool_call", {"run_id": "run", "tool": "Edit", "params": changes[1]}),
        ("subagent_tool_result", {"run_id": "run", "tool": "Edit", "success": True, "data": "第二次写入"}),
        ("subagent_subagent_done", {"run_id": "run", "preset": "chapter_polisher", "result": "修改完成"}),
    ]
    with sqlite3.connect(trace) as db:
        db.executescript("""
            CREATE TABLE traces (trace_id TEXT,session_id TEXT,project_id TEXT,source_agent TEXT,
              source TEXT,status TEXT,user_message TEXT,final_answer TEXT,started_at TEXT,turn_no INTEGER);
            CREATE TABLE trace_events (event_id TEXT,trace_id TEXT,sequence_no INTEGER,event_type TEXT,
              payload_json TEXT,created_at TEXT);
        """)
        for tid, started in (("trace-a", "2026-01-01T00:00:00+00:00"), ("trace-b", "2026-01-01T00:01:00+00:00")):
            db.execute("INSERT INTO traces VALUES (?, 'session','project','main_agent','live','completed','修改正文','最终回复',?,1)", (tid, started))
        for seq, (kind, payload) in enumerate(sources, 1):
            db.execute("INSERT INTO trace_events VALUES (?, 'trace-a', ?, ?, ?, ?)",
                       (f"source-{seq}", seq, kind, json.dumps(payload, ensure_ascii=False), f"2026-01-01T00:00:{seq:02}+00:00"))
        db.execute("INSERT INTO trace_events VALUES ('final-source','trace-b',1,'assistant_turn',?, '2026-01-01T00:01:01+00:00')",
                   (json.dumps({"reasoning_content": "已核验", "content": "最终回复"}, ensure_ascii=False),))
    store = DocumentHistoryStore(project)
    root = store.start_request("trace-a", "session", "修改正文")
    store.append_request_event("trace-a", "main_agent_thinking", "先确认要求")
    store.append_request_event("trace-a", "main_agent_output", "请确认")
    store.append_request_event("trace-a", "main_agent_thinking", "已理解回答")
    store.append_request_event("trace-a", "main_delegation", "修改章节")
    store.append_request_event("trace-a", "main_agent_thinking", "已核验")
    store.append_request_event("trace-a", "main_agent_output", "最终回复")
    child = store.run_request_id("trace-a", "run")
    store.start_request(child, "session", "修改正文", parent_request_id="trace-a")
    store.append_request_event(child, "main_delegation", "修改章节")
    for params, result in zip(changes, ("第一次写入", "第二次写入")):
        store.append_document_event(child, path, "file_change_attempt", json.dumps({
            "run_id": "run", "tool": "Edit", "params": params, "success": True, "result": result}, ensure_ascii=False))
    aggregate = "aggregate-thinking"
    store.append_document_event(child, path, "chapter_polisher_thinking", "第一次思考第二次思考", event_id=aggregate)
    store.append_document_event(child, path, "chapter_polisher_output", "修改完成")
    store.close_request(child, "completed", "修改完成", ["trace-a"], 1)
    store.close_request("trace-a", "completed", "最终回复", ["trace-a", "trace-b"], 1)
    migrate_requirements(trace, project, apply=True)
    return trace, project, store, root, store._unit_id(child, "document", path), aggregate


def test_offline_repair_orders_cross_trace_questions_and_splits_thinking_between_writes(tmp_path):
    trace, project, store, root, document, aggregate = _fixture(tmp_path)
    original = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    dry = repair_history_order(trace, project)
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == original
    assert dry["split_parts"] == 2
    with sqlite3.connect(store.db_path) as db:
        before = db.execute("SELECT * FROM history_events ORDER BY rowid").fetchall()
        units = db.execute("SELECT * FROM history_units ORDER BY rowid").fetchall()
    result = repair_history_order(trace, project, apply=True)
    assert result["changed"] > 0
    unit = store.get_unit(root)
    assert [e["kind"] for e in unit["events"]] == [
        "user_input", "main_agent_thinking", "main_agent_output", "question_ask", "user_answer",
        "main_agent_thinking", "main_delegation", "main_agent_thinking", "main_agent_output"]
    assert unit["events"][-1]["content"] == "最终回复"
    assert len([e for e in unit["archived_events"] if e["kind"] == "question_ask"]) == 1
    unit = store.get_unit(document)
    local = [e for e in unit["events"] if not e.get("inherited_requirement")]
    assert [e["kind"] for e in local] == ["main_delegation", "chapter_polisher_thinking",
        "file_change_attempt", "chapter_polisher_thinking", "file_change_attempt", "chapter_polisher_output"]
    assert [e["content"] for e in local if e["kind"] == "chapter_polisher_thinking"] == ["第一次思考", "第二次思考"]
    assert all(e.get("original_event_id") == aggregate for e in local if e["kind"] == "chapter_polisher_thinking")
    assert any(e["event_id"] == aggregate for e in unit["archived_events"])
    view = HistoryAnalyzer._evidence_view(unit)
    assert [e["event_order"] for e in view["events"]] == sorted(e["event_order"] for e in view["events"])
    with sqlite3.connect(store.db_path) as db:
        after = db.execute("SELECT * FROM history_events ORDER BY rowid").fetchall()
        assert after[:len(before)] == before
        assert db.execute("SELECT * FROM history_units ORDER BY rowid").fetchall() == units
    assert repair_history_order(trace, project, apply=True)["changed"] == 0


def test_unrecoverable_evidence_is_retained_and_marked_not_guessed(tmp_path):
    trace, project, store, root, _, _ = _fixture(tmp_path)
    unknown = store.append_request_event("trace-a", "main_agent_thinking", "没有原始来源")
    repair_history_order(trace, project, apply=True)
    unit = store.get_unit(root)
    assert unit["events"][-1]["event_id"] == unknown
    assert unit["events"][-1]["order_status"] == "unverified"
    assert unit["events"][-1]["event_order"] is None


def test_live_history_without_offline_metadata_keeps_existing_behavior(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("live", "session", "需求")
    store.append_request_event("live", "main_agent_thinking", "思考")
    store.append_request_event("live", "main_agent_output", "回复")
    unit = store.get_unit(root)
    assert [e["kind"] for e in unit["events"]] == ["user_input", "main_agent_thinking", "main_agent_output"]
    assert not any(e.get("order_status") for e in unit["events"])


def test_copied_delegation_is_archived_but_a_real_repeated_ask_remains(tmp_path):
    trace, project, store, root, document, _ = _fixture(tmp_path)
    child = store.get_unit(document)["request_id"]
    duplicate = store.append_request_event(child, "main_delegation", "修改章节")
    with sqlite3.connect(trace) as db:
        call = json.loads(db.execute("SELECT payload_json FROM trace_events WHERE event_id='source-3'").fetchone()[0])
        db.execute("UPDATE trace_events SET sequence_no=sequence_no+10 WHERE trace_id='trace-a' AND sequence_no>=7")
        for seq, kind, payload in (
            (7, "tool_call", call),
            (8, "question_ask", {"questions": call["params"]["questions"]}),
            (9, "user_answer", {"answers": ["克制"]}),
            (10, "tool_result", {"tool": "AskUserQuestion", "success": True, "data": ["克制"]}),
        ):
            db.execute("INSERT INTO trace_events VALUES (?, 'trace-a', ?, ?, ?, ?)",
                       (f"repeat-{seq}",seq,kind,json.dumps(payload,ensure_ascii=False),f"2026-01-01T00:00:{seq:02}+00:00"))
    migrate_requirements(trace, project, apply=True)
    repair_history_order(trace, project, apply=True)
    unit = store.get_unit(root)
    questions = [e for e in unit["events"] if e["kind"] == "question_ask"]
    answers = [e for e in unit["events"] if e["kind"] == "user_answer"]
    assert len(questions) == len(answers) == 2
    assert questions[0]["event_order"] < answers[0]["event_order"] < questions[1]["event_order"] < answers[1]["event_order"]
    unit = store.get_unit(document)
    assert duplicate not in {e["event_id"] for e in unit["events"]}
    assert duplicate in {e["event_id"] for e in unit["archived_events"]}
    assert repair_history_order(trace, project)["changed"] == 0


def test_repeated_answers_without_user_answer_events_and_unpaired_questions_are_not_collapsed():
    question = [{"question": "如何修改？"}]
    rows = [
        ("question_ask", {"questions": question}),
        ("question_ask", {"questions": question}),
        ("subagent_tool_call", {"run_id": "run", "tool": "AskUserQuestion", "params": {"questions": question}}),
        ("subagent_tool_result", {"run_id": "run", "tool": "AskUserQuestion", "success": True, "data": ["克制"]}),
        ("subagent_tool_call", {"run_id": "run", "tool": "AskUserQuestion", "params": {"questions": question}}),
        ("subagent_tool_result", {"run_id": "run", "tool": "AskUserQuestion", "success": True, "data": ["克制"]}),
    ]
    candidates = _source_evidence({"events": [
        {"event_id": str(i), "event_type": kind, "payload": payload}
        for i, (kind, payload) in enumerate(rows)]})
    assert len([c for c in candidates if c["kind"] == "question_ask"]) == 4
    answers = [c for c in candidates if c["kind"] == "user_answer"]
    assert len(answers) == 2 and answers[0]["order"] < answers[1]["order"]
