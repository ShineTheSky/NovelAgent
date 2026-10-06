import asyncio
import hashlib
import json
import sqlite3

import pytest

from novelagent.history import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore


def _execution(store, root_id, root_request, run, path, refs):
    request = store.run_request_id(root_request, run)
    store.start_request(request, "session", "", parent_request_id=root_request)
    store.set_requirement_refs(request, root_id, refs)
    store.append_document_event(request, path, "polisher_thinking", "使用动作替换直白解释")
    store.close_request(request, "completed", "已修改", [root_request], 3)
    hid = store._unit_id(request, "document", path)
    event = next(e["event_id"] for e in store.get_unit(hid)["events"] if e["kind"] == "polisher_thinking")
    return request, hid, event


def test_shared_requirement_is_stored_once_and_late_answer_does_not_change_earlier_history(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "克制表达", [{"role": "user", "content": "原始上下文"}])
    original = f"{root}:input"
    _, before, _ = _execution(store, root, "root", "before", "chapters/a.md", [original])
    answer = store.append_request_event("root", "user_answer", '["保留鞋子来历"]')
    _, after, _ = _execution(store, root, "root", "after", "chapters/a.md", [original, answer])
    _, other, _ = _execution(store, root, "root", "other", "world/b.md", [original])
    store.update_request_context("root", [{"role": "assistant", "content": "后续上下文"}])
    closed = store.close_request("root", "completed", "完成", ["root"], 3)
    assert set(closed) == {root, before, after, other}
    root_unit = store.get_unit(root)
    assert root_unit["kind"] == "requirement"
    assert {row["history_id"] for row in root_unit["affected_files"]} == {before, after, other}
    assert answer not in {e["event_id"] for e in store.get_unit(before)["events"]}
    assert answer not in {e["event_id"] for e in store.get_unit(other)["events"]}
    assert answer in {e["event_id"] for e in store.get_unit(after)["events"]}
    assert store.get_unit(after)["requirement_history_id"] == root
    assert HistoryAnalyzer._evidence_view(root_unit)["initial_model_context"][0]["content"] == "原始上下文"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM history_events WHERE kind = 'user_answer'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM history_events WHERE kind = 'user_input' AND content != ''").fetchone()[0] == 1


def test_requirement_refs_reject_wrong_owner_and_roll_back(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "需求")
    other = store.start_request("other", "session", "其他需求")
    with pytest.raises(ValueError):
        store.set_requirement_refs("root", root, [f"{other}:input"])
    assert store.request_requirement_refs("root") == []


def test_one_execution_can_keep_different_requirement_refs_for_each_file(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "修改两个文件")
    answer = store.append_request_event("root", "user_answer", "只对第一份补充")
    request, first, _ = _execution(store, root, "root", "run", "chapters/a.md", [f"{root}:input", answer])
    store.append_document_event(request, "chapters/b.md", "polisher_output", "已修改第二份")
    second = store._unit_id(request, "document", "chapters/b.md")
    store.set_requirement_refs(request, root, [f"{root}:input"], path="chapters/b.md")
    store.append_document_event(request, "chapters/b.md", "polisher_thinking", "只采用原需求")
    assert answer in store.get_unit(first)["requirement_event_ids"]
    assert answer not in store.get_unit(second)["requirement_event_ids"]


def test_file_analysis_cannot_repeat_shared_preferences_and_mixed_evidence_has_correct_owners(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "克制表达")
    original = f"{root}:input"
    _, first, local = _execution(store, root, "root", "first", "chapters/a.md", [original])
    _, second, _ = _execution(store, root, "root", "second", "chapters/b.md", [original])
    store.close_request("root", "completed", "完成", ["root"], 3)
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    files = FileLifecycleStore(str(tmp_path), "project")
    candidate = {"layer": "memory", "title": "克制", "claim": "克制表达", "source_event_ids": [original]}
    for hid in (first, second):
        analyzer._apply("project", store.get_unit(hid), {"records": [candidate]})
    assert files.list("memory") == []
    analyzer._apply("project", store.get_unit(root), {"records": [candidate]})
    preference = files.list("memory")[0]
    assert preference["history_ids"] == [root]
    assert preference["support_count"] == 1
    for hid in (first, second):
        unit = store.get_unit(hid)
        own = next(e["event_id"] for e in unit["events"] if e["kind"] == "polisher_thinking")
        analyzer._apply("project", unit, {"records": [{
            **candidate, "source_event_ids": [original, own], "relation": "support",
            "related_layer": "memory", "related_id": preference["id"],
        }]})
    assert files.get("memory", preference["id"])["support_count"] == 1
    analyzer._apply("project", store.get_unit(first), {"records": [{
        **candidate, "claim": "用动作落实克制需求", "source_event_ids": [original, local],
    }]})
    mixed = next(m for m in files.list("memory") if m["claim"] == "用动作落实克制需求")
    assert mixed["history_ids"] == [root]
    assert {e["history_id"]: e["history_event_ids"] for e in mixed["semantic_evidence"]} == {root: [original, local]}


def test_same_requirement_cannot_add_multiple_supports(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "克制表达")
    store.append_document_event("root", "chapters/a.md", "polisher_output", "修改")
    store.close_request("root", "completed", "完成")
    files = FileLifecycleStore(str(tmp_path), "project")
    previous = files.write("memory", {"claim": "克制表达", "weight": 65, "support_count": 1, "support_sources": ["old"]})
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    record = {"layer": "memory", "claim": "克制表达", "source_event_ids": [f"{root}:input"],
              "relation": "support", "related_layer": "memory", "related_id": previous["id"]}
    analyzer._apply("project", store.get_unit(root), {"records": [record, record]})
    saved = files.get("memory", previous["id"])
    assert saved["support_count"] == 2 and saved["weight"] == 80
    analyzer._apply("project", store.get_unit(root), {"records": [record, record]})
    assert files.get("memory", previous["id"])["support_count"] == 2


def test_failed_files_do_not_schedule_requirement_analysis(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "修改")
    store.append_document_event("root", "chapters/a.md", "tool_failure", "失败")
    assert store.close_request("root", "completed", "失败") == []
    assert store.get_unit(root)["analysis_status"] == "deferred"
    assert store.list_analyzable() == []


def test_completed_child_is_not_analyzed_before_root_closes_or_after_root_fails(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "修改")
    _, child, _ = _execution(store, root, "root", "child", "chapters/a.md", [f"{root}:input"])
    assert store.get_unit(child)["status"] == "completed"
    assert store.list_analyzable() == []
    store.close_request("root", "interrupted", "中断")
    assert store.list_analyzable() == []
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    asyncio.run(analyzer.analyze("project", child))
    assert FileLifecycleStore(str(tmp_path), "project").list("memory") == []
