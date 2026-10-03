import hashlib
import json
import sqlite3

import pytest
import yaml

from novelagent.history import DocumentHistoryStore
from novelagent.history.memory_links import link_memories, link_records, map_trace_sources, migrate_requirements


@pytest.fixture
def evidence(tmp_path):
    project = tmp_path / "workspace" / "project-a"
    project.mkdir(parents=True)
    trace_db = tmp_path / "trace.db"
    with sqlite3.connect(trace_db) as db:
        db.executescript("""
            CREATE TABLE traces (trace_id TEXT, session_id TEXT, project_id TEXT,
                source_agent TEXT, source TEXT, status TEXT, user_message TEXT,
                final_answer TEXT, started_at TEXT, turn_no INTEGER);
            CREATE TABLE trace_events (event_id TEXT, trace_id TEXT,
                sequence_no INTEGER, event_type TEXT, payload_json TEXT);
        """)
        for trace, turn in (("trace-a", 24), ("trace-b", 25), ("trace-chat", 26)):
            db.execute("INSERT INTO traces VALUES (?, 'session-a', 'project-a', 'main_agent', 'live', 'completed', ?, '完成', ?, ?)",
                       (trace, "修改正文" if trace != "trace-chat" else "讨论设定", f"2026-01-{turn}", turn))
        for sid, trace, seq, kind, payload in (
            ("user-a", "trace-a", 1, "user_message", {"content": "修改正文", "turn": 1}),
            ("ask-a", "trace-a", 5, "user_answer", {"answers": ["保留鞋子来历"]}),
            ("ask-result", "trace-a", 6, "tool_result", {"tool": "AskUserQuestion", "success": True, "data": ["保留鞋子来历"], "turn": 7}),
            ("edit-a", "trace-a", 7, "subagent_tool_call", {"run_id": "after", "tool": "Edit", "turn": 7,
                "params": {"path": "chapters/a.md", "old_string": "旧", "new_string": "新"}}),
            ("read-a", "trace-a", 8, "tool_call", {"tool": "Read", "params": {"path": "chapters/a.md"}}),
            ("user-b", "trace-b", 1, "user_message", {"content": "修改正文", "turn": 1}),
            ("user-chat", "trace-chat", 1, "user_message", {"content": "讨论设定"}),
        ):
            db.execute("INSERT INTO trace_events VALUES (?, ?, ?, ?, ?)",
                       (sid, trace, seq, kind, json.dumps(payload, ensure_ascii=False)))
    store = DocumentHistoryStore(project)
    histories = {}
    for trace in ("trace-a", "trace-b"):
        histories[trace] = store.start_request(trace, "session-a", "修改正文")
    for trace, turn, run, path in (
        ("trace-a", 24, "before", "chapters/a.md"),
        ("trace-a", 24, "after", "chapters/a.md"),
        ("trace-a", 24, "after", "world/b.md"),
        ("trace-b", 25, "later", "chapters/a.md"),
    ):
        request = store.run_request_id(trace, run)
        store.start_request(request, "session-a", "修改正文", parent_request_id=trace)
        if run == "after":
            store.append_request_event(request, "user_answer", json.dumps(["保留鞋子来历"], ensure_ascii=False),
                                       event_id=f"{request}:answer")
        store.append_document_event(request, path, "file_change_attempt", json.dumps({
            "run_id": run, "tool": "Edit", "params": {"path": path, "old_string": "旧", "new_string": "新"},
            "success": True, "result": path,
        }, ensure_ascii=False))
        store.close_request(request, "completed", "完成", [trace], turn)
        histories[(run, path)] = store._unit_id(request, "document", path)
    for trace, turn in (("trace-a", 24), ("trace-b", 25)):
        store.close_request(trace, "completed", "完成", [trace], turn)
    chat = store.start_request("trace-chat", "session-a", "讨论设定")
    store.close_request("trace-chat", "completed", "完成", ["trace-chat"], 26)
    histories["chat"] = chat
    return trace_db, project, store, histories


def test_sources_scope_history_by_request_run_and_file_not_react_turn(evidence):
    trace_db, project, store, histories = evidence
    links = map_trace_sources(trace_db, project, {"edit-a", "ask-a", "ask-result", "user-a"})
    assert [link["history_id"] for link in links["edit-a"]] == [histories[("after", "chapters/a.md")]]
    assert links["ask-a"][0]["match"] == "evidence"
    assert {link["history_id"] for link in links["ask-a"]} == {
        histories[("after", "chapters/a.md")], histories[("after", "world/b.md")]}
    assert {link["history_id"] for link in links["ask-result"]} == {link["history_id"] for link in links["ask-a"]}
    assert histories[("before", "chapters/a.md")] not in {link["history_id"] for link in links["ask-a"]}
    assert histories[("later", "chapters/a.md")] not in {link["history_id"] for link in links["user-a"]}
    for group in links.values():
        for link in group:
            assert set(link["history_event_ids"]) <= {e["event_id"] for e in store.get_unit(link["history_id"])["events"]}


def test_read_is_context_only_and_missing_sources_are_not_guessed(evidence):
    trace_db, project, _, histories = evidence
    links = map_trace_sources(trace_db, project, {"read-a", "missing"})
    assert links["missing"] == []
    assert {link["history_id"] for link in links["read-a"]} == {
        histories[("before", "chapters/a.md")], histories[("after", "chapters/a.md")]}
    assert all(link["match"] == "context" and not link["history_event_ids"] for link in links["read-a"])


def test_conversation_maps_to_conversation_history(evidence):
    trace_db, project, _, histories = evidence
    links = map_trace_sources(trace_db, project, {"user-chat"})
    assert [link["history_id"] for link in links["user-chat"]] == [histories["chat"]]


def test_apply_preserves_memory_and_databases_and_is_idempotent(evidence):
    trace_db, project, store, histories = evidence
    path = project / ".memory" / "reference" / "mem_test.md"
    path.parent.mkdir(parents=True)
    original = {"id": "mem_test", "title": "反馈", "claim": "旧有结论", "category": "reference",
                "weight": 65, "support_count": 2, "updated": "2026-01-01", "created": "2025-01-01",
                "trace_refs": [{"trace_id": "trace-a", "turn": 7, "source_event_ids": ["ask-a", "read-a"]}],
                "source_event_ids": ["user-b"], "semantic_evidence": [{"why": "保留原语义证据"}],
                "history_ids": [histories["chat"]], "history_event_ids": ["previous-evidence"]}
    body = "\n\n原有正文\n\n  保留格式。\n"
    path.write_text("---\n" + yaml.safe_dump(original, allow_unicode=True) + "---" + body, encoding="utf-8")
    raw = path.read_bytes()
    fingerprints = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (trace_db, store.db_path)}
    dry = link_memories(trace_db, project)
    assert dry["changed"] == 1 and path.read_bytes() == raw
    applied = link_memories(trace_db, project, apply=True)
    assert applied["changed"] == applied["linked"] == 1
    text = path.read_text(encoding="utf-8")
    _, header, new_body = text.split("---", 2)
    saved = yaml.safe_load(header)
    assert new_body == body
    assert {key: saved[key] for key in original if key not in {"history_ids", "history_event_ids"}} == {
        key: value for key, value in original.items() if key not in {"history_ids", "history_event_ids"}}
    assert histories["chat"] in saved["history_ids"]
    assert histories[("later", "chapters/a.md")] in saved["history_ids"]
    assert saved["history_event_ids"][0] == "previous-evidence"
    assert applied["items"][0]["context_only_source_event_ids"] == ["read-a"]
    assert applied["items"][0]["unmapped_source_event_ids"] == []
    assert link_memories(trace_db, project, apply=True)["changed"] == 0
    assert path.read_text(encoding="utf-8") == text
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == fingerprint for p, fingerprint in fingerprints.items())


def test_missing_history_does_not_create_database(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        map_trace_sources(tmp_path / "missing.db", project, {"missing"})
    assert not (tmp_path / "missing.db").exists()
    assert not (project / ".history").exists()


def test_migration_replaces_fanout_with_canonical_requirement_evidence(evidence):
    trace_db, project, store, histories = evidence
    path = project / ".memory/project/mem_migrate.md"
    path.parent.mkdir(parents=True)
    original = {"id": "mem_migrate", "claim": "保留鞋子来历", "weight": 65,
                "support_count": 1, "source_event_ids": ["ask-a", "ask-result", "read-a"]}
    path.write_text("---\n" + yaml.safe_dump(original, allow_unicode=True) + "---\n\n原文\n", encoding="utf-8")
    link_memories(trace_db, project, apply=True)
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    assert migrate_requirements(trace_db, project)["file_histories"] == 1
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    migrate_requirements(trace_db, project, apply=True)
    links = map_trace_sources(trace_db, project, {"ask-a", "ask-result"})
    assert links["ask-a"][0]["history_id"] == histories["trace-a"]
    assert links["ask-a"][0]["history_event_ids"] == links["ask-result"][0]["history_event_ids"]
    applied = link_memories(trace_db, project, apply=True, replace_legacy_links=True)
    saved = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert saved["history_ids"] == [histories["trace-a"]]
    assert saved["weight"] == 65 and saved["support_count"] == 1
    # The "before" run has no retained modification in Trace and was not
    # linked by migration; keep that standalone file instead of guessing.
    assert saved["related_history_ids"] == [histories[("before", "chapters/a.md")]]
    assert store.get_unit(histories[("after", "chapters/a.md")])["requirement_history_id"] == histories["trace-a"]
    assert len([e for e in store.get_unit(histories["trace-a"])["events"] if e["kind"] == "user_answer"]) == 1
    assert applied["changed"] == 1
    migrate_requirements(trace_db, project, apply=True)
    assert link_memories(trace_db, project, apply=True, replace_legacy_links=True)["changed"] == 0


def test_migration_distinguishes_identical_answers_at_different_times(evidence):
    trace_db, project, store, histories = evidence
    with sqlite3.connect(trace_db) as db:
        db.execute("INSERT INTO trace_events VALUES ('ask-late', 'trace-a', 9, 'user_answer', ?)",
                   (json.dumps({"answers": ["保留鞋子来历"]}, ensure_ascii=False),))
    migrate_requirements(trace_db, project, apply=True)
    mapped = map_trace_sources(trace_db, project, {"ask-a", "ask-late"})
    first = mapped["ask-a"][0]["history_event_ids"][0]
    late = mapped["ask-late"][0]["history_event_ids"][0]
    assert first != late
    adopted = store.get_unit(histories[("after", "chapters/a.md")])["requirement_event_ids"]
    assert first in adopted and late not in adopted


def test_migration_preserves_existing_canonical_history_event_ids(evidence):
    trace_db, project, store, histories = evidence
    original = f"{histories['trace-a']}:input"
    assert original in {e["event_id"] for e in store.get_unit(histories["trace-a"])["events"]}
    migrate_requirements(trace_db, project, apply=True)
    assert original in {e["event_id"] for e in store.get_unit(histories["trace-a"])["events"]}
    assert map_trace_sources(trace_db, project, {"user-a"})["user-a"][0]["history_event_ids"] == [original]


@pytest.mark.parametrize("layer,prefix", [("insight", "ins"), ("insight", "evi"), ("pattern", "pat"), ("memory", "pat")])
def test_all_derived_layers_link_exact_history_without_changing_claims(evidence, layer, prefix):
    trace_db, project, _, histories = evidence
    migrate_requirements(trace_db, project, apply=True)
    path = project / f".{layer}/agent/{prefix}_test.md"
    path.parent.mkdir(parents=True)
    original = {"id": f"{prefix}_test", "category": "agent", "claim": "原有结论", "weight": 65,
                "support_count": 2, "created": "2025-01-01", "updated": "2026-01-01",
                "source_event_ids": ["ask-a"], "trace_ids": ["trace-a"],
                "semantic_evidence": [{"why": "原有语义证据"}]}
    body = "\n\n原有正文。\n"
    path.write_text("---\n" + yaml.safe_dump(original, allow_unicode=True) + "---" + body, encoding="utf-8")
    raw = path.read_bytes()
    index = path.parent / "notes.md"
    index.write_text("普通文档，不是派生记录。", encoding="utf-8")
    assert link_records(trace_db, project, layers=(layer,))["changed"] == 1
    assert path.read_bytes() == raw
    report = link_records(trace_db, project, layers=(layer,), apply=True)
    assert report["layers"][layer] == {"records": 1, "linked": 1, "changed": 1}
    _, header, saved_body = path.read_text(encoding="utf-8").split("---", 2)
    saved = yaml.safe_load(header)
    assert {key: saved[key] for key in original} == original
    assert saved_body == body
    assert saved["history_ids"] == [histories["trace-a"]]
    assert saved["history_event_ids"]
    assert report["items"][0]["unmapped_source_event_ids"] == []
    assert link_records(trace_db, project, layers=(layer,), apply=True)["changed"] == 0


def test_legacy_pattern_inherits_history_native_memory_without_trace_sources(evidence):
    trace_db, project, _, histories = evidence
    memory = project / ".memory/project/mem_native.md"
    pattern = project / ".pattern/project/pat_legacy.md"
    memory.parent.mkdir(parents=True)
    pattern.parent.mkdir(parents=True)
    direct = histories["chat"]
    related = histories[("before", "chapters/a.md")]
    link = {"source_event_id": "user-chat", "history_id": direct,
            "history_event_ids": [f"{direct}:input"], "match": "evidence"}
    memory.write_text("---\n" + yaml.safe_dump({
        "id": "mem_native", "history_ids": [direct], "history_event_ids": [f"{direct}:input"],
        "history_source_links": [link], "related_history_ids": [related],
    }) + "---\n原有记忆\n", encoding="utf-8")
    pattern.write_text("---\n" + yaml.safe_dump({
        "id": "pat_legacy", "memory_ids": ["mem_native", "mem_missing"], "weight": 230,
        "support_count": 4,
    }) + "---\n原有模式\n", encoding="utf-8")
    raw_memory = memory.read_bytes()
    report = link_records(trace_db, project, layers=("pattern",), apply=True)
    saved = yaml.safe_load(pattern.read_text(encoding="utf-8").split("---", 2)[1])
    assert saved["history_ids"] == [direct]
    assert saved["history_event_ids"] == [f"{direct}:input"]
    assert saved["history_source_links"] == [link]
    assert saved["related_history_ids"] == [related]
    assert saved["weight"] == 230 and saved["support_count"] == 4
    assert report["items"][0]["missing_memory_ids"] == ["mem_missing"]
    assert memory.read_bytes() == raw_memory
    assert link_records(trace_db, project, layers=("pattern",), apply=True)["changed"] == 0


def test_main_agent_evidence_maps_root_and_actual_subagent_run(evidence):
    trace_db, project, store, histories = evidence
    with sqlite3.connect(trace_db) as db:
        for sid, seq, kind, payload in (
            ("main-turn", 10, "assistant_turn", {"reasoning_content": "判断需求", "content": "交给润色"}),
            ("main-spawn", 11, "tool_call", {"tool": "SubAgent", "params": {"task": "任务原文"}}),
            ("run-start", 12, "subagent_subagent_start", {"run_id": "after"}),
            ("main-result", 13, "tool_result", {"tool": "SubAgent", "success": True, "data": "已润色"}),
        ):
            db.execute("INSERT INTO trace_events VALUES (?, 'trace-a', ?, ?, ?)",
                       (sid, seq, kind, json.dumps(payload, ensure_ascii=False)))
    store.append_request_event("trace-a", "main_agent_thinking", "判断需求")
    store.append_request_event("trace-a", "main_agent_output", "交给润色")
    store.append_request_event("trace-a", "main_delegation", "任务原文")
    for run in ("before", "after"):
        store.append_document_event(store.run_request_id("trace-a", run), "chapters/a.md",
                                    "chapter_polisher_output", "已润色")
    links = map_trace_sources(trace_db, project, {"main-turn", "main-spawn", "main-result"})
    assert {link["history_id"] for link in links["main-turn"]} == {histories["trace-a"]}
    assert len(links["main-turn"][0]["history_event_ids"]) == 2
    assert {link["history_id"] for link in links["main-spawn"]} == {histories["trace-a"]}
    assert {link["history_id"] for link in links["main-result"]} == {histories[("after", "chapters/a.md")]}
    assert all(link["match"] == "evidence" for group in links.values() for link in group)


@pytest.mark.parametrize("layer", ["insight", "memory", "pattern"])
def test_native_records_migrate_to_roots_without_trace_sources_or_evidence_rewrite(evidence, layer):
    trace_db, project, store, histories = evidence
    migrate_requirements(trace_db, project, apply=True)
    store.set_requirement_refs(store.run_request_id("trace-a", "before"), histories["trace-a"],
                               [f"{histories['trace-a']}:input"])
    first = histories[("after", "chapters/a.md")]
    other = histories[("before", "chapters/a.md")]
    path = project / f".{layer}/project/native.md"
    path.parent.mkdir(parents=True)
    original = {"id": "native", "history_ids": [first, other, histories["chat"]],
                "related_history_ids": [other], "history_event_ids": ["original-event"],
                "semantic_evidence": [{"history_id": first, "history_event_ids": ["original-event"]}],
                "history_source_links": [{"history_id": first, "history_event_ids": ["original-event"], "match": "evidence"}],
                "weight": 65, "support_count": 2, "updated": "2026-01-01"}
    body = "\n\n原有正文\n"
    path.write_text("---\n" + yaml.safe_dump(original, allow_unicode=True) + "---" + body, encoding="utf-8")
    fingerprint = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    assert link_records(trace_db, project, layers=(layer,))["changed"] == 1
    assert link_records(trace_db, project, layers=(layer,), apply=True)["changed"] == 1
    _, header, saved_body = path.read_text(encoding="utf-8").split("---", 2)
    saved = yaml.safe_load(header)
    assert saved["history_ids"] == [histories["trace-a"], histories["chat"]]
    assert saved["related_history_ids"] == []
    assert {k: saved[k] for k in original if k not in {"history_ids", "related_history_ids"}} == {
        k: v for k, v in original.items() if k not in {"history_ids", "related_history_ids"}}
    assert saved_body == body
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == fingerprint
    assert link_records(trace_db, project, layers=(layer,), apply=True)["changed"] == 0
