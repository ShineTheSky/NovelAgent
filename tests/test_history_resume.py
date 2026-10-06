import json
import sqlite3
from types import SimpleNamespace

import pytest

from novelagent.history.store import DocumentHistoryStore
from novelagent.history.sources import root_history_ids
from novelagent.memory.consolidation import KnowledgePipeline


def attempt(store, request, status, text="继续克制表达", path="chapters/a.md"):
    root = store.start_request(request, "session", text)
    child = request + ":writer"
    store.start_request(child, "session", "写作任务", parent_request_id=request)
    store.set_requirement_refs(child, root, [root + ":input"])
    store.append_document_event(child, path, "writer_thinking", "用动作代替解释")
    store.close_request(child, status, "文件修改结果")
    store.close_request(request, status, "完成" if status == "completed" else "被中断")
    return root, store._unit_id(child, "document", path)


def merge(store, old, new):
    return store.merge_roots(old, new, "明确继续未完成需求", {hid: store.root_signature(hid) for hid in (old, new)})


def test_merge_preserves_file_history_events_and_root_identity(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    old, file1 = attempt(store, "old", "interrupted")
    new, file2 = attempt(store, "new", "completed")
    before = store.get_unit(file1)
    with sqlite3.connect(store.db_path) as db:
        events = db.execute("SELECT * FROM history_events ORDER BY rowid").fetchall()
    assert merge(store, old, new) == old
    root = store.get_unit(new)
    assert root["history_id"] == old and root["requested_history_id"] == new
    assert root["status"] == "completed"
    assert [a["history_id"] for a in root["attempts"]] == [old, new]
    assert [e["history_id"] for e in root["events"]] == [old, old, new, new]
    assert [f["history_id"] for f in root["affected_files"]] == [file1, file2]
    assert store.get_unit(file1)["events"] == before["events"]
    assert store.get_unit(file2)["requirement_history_id"] == new
    assert store.get_unit(file2)["canonical_requirement_history_id"] == old
    assert root_history_ids(tmp_path, [file1, new, file2]) == [old]
    assert store.list_units(kind="requirement")["total"] == 1
    assert store.list_units(kind="requirement")["items"][0]["status"] == "completed"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT * FROM history_events ORDER BY rowid").fetchall() == events


def test_multiple_interruptions_flatten_to_same_root_and_cannot_cycle(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    one, _ = attempt(store, "one", "failed")
    two, _ = attempt(store, "two", "interrupted")
    merge(store, one, two)
    three, _ = attempt(store, "three", "completed")
    assert [c["history_id"] for c in store.resume_candidates(three)] == [one]
    merge(store, one, three)
    assert len(store.get_unit(three)["attempts"]) == 3
    assert merge(store, one, three) == one  # idempotent
    with pytest.raises(ValueError):
        merge(store, three, one)


def test_changed_evidence_blocks_stale_resume_decision(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    one, _ = attempt(store, "one", "failed")
    two, _ = attempt(store, "two", "completed")
    signatures = {hid: store.root_signature(hid) for hid in (one, two)}
    store.append_request_event("two", "user_answer", "增加一条新的要求")
    with pytest.raises(ValueError, match="evidence changed"):
        store.merge_roots(one, two, "相同", signatures)
    assert store.canonical_root(two) == two


def test_completed_history_is_not_a_resume_candidate(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    one, _ = attempt(store, "one", "completed")
    two, _ = attempt(store, "two", "completed")
    assert store.resume_candidates(two) == []
    with pytest.raises(ValueError, match="abnormal"):
        merge(store, one, two)


def test_supplement_invalidates_canonical_analysis_without_rewriting_refs(tmp_path):
    store = DocumentHistoryStore(tmp_path)
    one, _ = attempt(store, "one", "failed")
    two, _ = attempt(store, "two", "completed")
    merge(store, one, two)
    store.set_analysis_status(one, "complete")
    store.append_request_event("two", "user_answer", "后来补充的评价")
    assert store.get_unit(one)["analysis_status"] == "pending"
    store.set_analysis_status(two, "complete")
    assert store.get_unit(one)["analysis_status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("same", [True, False])
async def test_llm_resume_judgment_is_persisted_and_only_explicit_match_merges(tmp_path, same):
    store = DocumentHistoryStore(tmp_path / "p")
    one, _ = attempt(store, "one", "interrupted")
    two, _ = attempt(store, "two", "completed")
    class LLM:
        calls = 0
        async def chat(self, **kwargs):
            self.calls += 1
            yield SimpleNamespace(type="text_delta", content=json.dumps({
                "same_requirement": same, "target_history_id": one if same else "", "reason": "依据用户输入"}))
    llm = LLM()
    pipeline = KnowledgePipeline(llm, tmp_path, None, "model")
    result = await pipeline.resume_history("p", store.get_unit(two))
    assert result["history_id"] == (one if same else two)
    await pipeline.resume_history("p", store.get_unit(two))
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_no_abnormal_history_means_no_extra_model_call(tmp_path):
    store = DocumentHistoryStore(tmp_path / "p")
    one, _ = attempt(store, "one", "completed")
    pipeline = KnowledgePipeline(None, tmp_path, None, "model")
    assert (await pipeline.resume_history("p", store.get_unit(one)))["history_id"] == one


def test_merged_memory_evidence_is_ordered_by_attempt_not_event_kind(tmp_path):
    store = DocumentHistoryStore(tmp_path / "p")
    one, first = attempt(store, "one", "interrupted")
    two, second = attempt(store, "two", "completed")
    merge(store, one, two)
    pipeline = KnowledgePipeline(None, tmp_path, None, "model")
    evidence, _ = pipeline._evidence("p", store.get_unit(one))
    assert [(e["history_id"], e["kind"]) for e in evidence] == [
        (one, "user_input"), (one, "writer_thinking"),
        (two, "user_input"), (two, "writer_thinking"),
    ]
    assert all(e["order_status"] == "sequence_only" for e in evidence)
