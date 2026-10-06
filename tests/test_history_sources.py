import asyncio
import hashlib
import sqlite3

import pytest

from novelagent.history import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.history.sources import normalize_history_roots, root_history_ids
from novelagent.trace.file_lifecycle import FileLifecycleStore


@pytest.fixture
def sources(tmp_path):
    project = tmp_path / "project"
    store = DocumentHistoryStore(project)
    roots, files, events = [], [], []
    for request in ("first", "second"):
        root = store.start_request(request, "session", "修改正文")
        roots.append(root)
        for run in ("a", "b"):
            child = store.run_request_id(request, run)
            store.start_request(child, "session", "", parent_request_id=request)
            store.set_requirement_refs(child, root, [f"{root}:input"])
            store.append_document_event(child, f"chapters/{run}.md", "polisher_thinking", "改用动作落实需求")
            store.close_request(child, "completed", "完成")
            hid = store._unit_id(child, "document", f"chapters/{run}.md")
            files.append(hid)
            events.append(next(e["event_id"] for e in store.get_unit(hid)["events"] if e["kind"] == "polisher_thinking"))
        store.close_request(request, "completed", "完成")
    conversation = store.start_request("chat", "session", "讨论设定")
    store.close_request("chat", "completed", "收到")
    store.start_request("legacy", "session", "旧文件修改", parent_request_id="missing-request")
    store.append_document_event("legacy", "chapters/legacy.md", "polisher_output", "旧正文")
    store.close_request("legacy", "completed", "完成")
    standalone = store._unit_id("legacy", "document", "chapters/legacy.md")
    return project, store, roots, files, events, conversation, standalone


def test_root_resolution_preserves_standalone_conversations_and_independent_roots(sources):
    project, store, roots, files, _, conversation, standalone = sources
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    assert root_history_ids(project, [files[0], files[1], roots[0], files[2], conversation, standalone, "missing"]) == [
        *roots, conversation, standalone, "missing"]
    assert normalize_history_roots(project, {
        "history_ids": [files[0], files[1]], "related_history_ids": [files[1], files[2], files[3]],
    }) == {"history_ids": [roots[0]], "related_history_ids": [roots[1]]}
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before


def test_missing_parent_is_not_invented_and_missing_database_is_not_created(sources, tmp_path):
    project, store, _, files, _, _, _ = sources
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE history_units SET requirement_history_id = 'missing-parent' WHERE history_id = ?", (files[0],))
    assert root_history_ids(project, [files[0]]) == [files[0]]
    empty = tmp_path / "no-history"
    assert root_history_ids(empty, ["old-id"]) == ["old-id"]
    assert not empty.exists()


@pytest.mark.parametrize("layer", ["insight", "memory", "pattern"])
def test_runtime_writes_only_roots_but_preserves_exact_evidence(sources, layer):
    project, _, roots, files, events, _, _ = sources
    lifecycle = FileLifecycleStore(str(project.parent), project.name)
    precise = [{"history_id": files[0], "history_event_ids": [events[0]], "why": "具体落实差异"}]
    links = [{"history_id": files[0], "history_event_ids": [events[0]], "match": "evidence"}]
    saved = lifecycle.write(layer, {
        "claim": "动作落实情绪", "weight": 65, "support_count": 2,
        "history_ids": [files[0], files[1], roots[0], files[2]],
        "related_history_ids": [files[1]], "history_event_ids": [events[0]],
        "semantic_evidence": precise, "history_source_links": links,
    })
    assert saved["history_ids"] == roots
    assert saved["related_history_ids"] == []
    assert saved["history_event_ids"] == [events[0]]
    assert saved["semantic_evidence"] == precise
    assert saved["history_source_links"] == links
    assert saved["weight"] == 65 and saved["support_count"] == 2
    if layer == "memory":
        pattern = lifecycle.promote_memory_to_pattern(saved, [])
        assert pattern["history_ids"] == roots
        assert pattern["semantic_evidence"] == precise
        assert lifecycle.downgrade("pattern", pattern["id"])["history_ids"] == roots


def test_history_analysis_can_read_exact_file_evidence_after_root_normalization(sources):
    project, store, roots, files, events, _, _ = sources
    lifecycle = FileLifecycleStore(str(project.parent), project.name)
    memory = lifecycle.write("memory", {
        "claim": "动作落实情绪", "history_ids": [files[0]],
        "semantic_evidence": [{"history_id": files[0], "history_event_ids": [events[0]]}],
    })
    assert memory["history_ids"] == [roots[0]]
    analyzer = HistoryAnalyzer(None, str(project.parent), None)
    calls = []

    async def respond(prompt, tag):
        calls.append((prompt, tag))
        if tag == ":history/extract":
            return {"needs_history_ids": [files[0]], "records": [{
                "layer": "insight", "claim": "动作表达可复用", "source_event_ids": [events[1]],
            }]}
        assert files[0] in prompt and "改用动作落实需求" in prompt
        return {"decisions": []}

    analyzer._chat_json = respond
    asyncio.run(analyzer.analyze(project.name, files[1]))
    assert [tag for _, tag in calls] == [":history/extract", ":history/reconcile"]
    insight = lifecycle.list("insight")[0]
    assert insight["history_ids"] == [roots[0]]
    assert insight["semantic_evidence"][0]["history_id"] == roots[0]
