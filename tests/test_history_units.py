import asyncio
import json

from novelagent.history import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore


def test_document_history_unit_collects_prewrite_feedback_and_versions(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    request_id = "trace-a"
    store.start_request(request_id, "session-a", "把这段写得更克制")
    store.append_request_event(request_id, "user_answer", json.dumps([{
        "selected_options": [{"label": "保留", "description": "保留现有伏笔"}],
    }], ensure_ascii=False))
    path = store.project_dir / "chapters" / "content_1.1.1.md"
    store.record_revision(path, "rev-1", None, "初稿", "初稿", evidence={
        "history_request_id": request_id, "user_input": "把这段写得更克制",
    })
    store.append_document_event(request_id, "chapters/content_1.1.1.md",
                                "reviewer_output", "情绪说明太直白")
    store.record_revision(path, "rev-2", "rev-1", "改稿", "改稿",
                          previous_body="初稿", previous_rendered="初稿", evidence={
                              "history_request_id": request_id,
                              "reviewer_output": "情绪说明太直白",
                          })
    ids = store.close_request(request_id, "completed", "已完成", ["trace-a", "trace-b"], 4)
    assert len(ids) == 1
    unit = store.get_unit(ids[0])
    assert unit["kind"] == "document"
    assert unit["session_turn_no"] == 4
    assert [row["revision_id"] for row in unit["revisions"]] == ["rev-1", "rev-2"]
    assert [e["kind"] for e in unit["events"]].count("user_answer") == 1
    assert unit["trace_ids"] == ["trace-a", "trace-b"]
    assert store.list_analyzable() == ids
    assert [unit["history_id"] for unit in store.list_document_units(
        "chapters/content_1.1.1.md",
    )] == ids

    class NoMemoryLLM:
        calls = 0

        async def chat(self, **_kwargs):
            self.calls += 1
            yield type("Chunk", (), {"type": "text_delta", "content": '{"records":[]}'})()

    llm = NoMemoryLLM()
    asyncio.run(HistoryAnalyzer(llm, str(tmp_path), None).analyze("project-a", ids[0]))
    assert llm.calls == 1


def test_one_request_with_two_files_creates_two_units_after_all_asks(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    store.start_request("trace-many", "session-a", "修改卷纲和章纲")
    store.append_request_event("trace-many", "user_answer", "保持两条时间线")
    for path, revision in (("outlines/outline_1.0.0.md", "volume-rev"),
                           ("outlines/outline_1.1.0.md", "chapter-rev")):
        store.record_revision(store.project_dir / path, revision, None, path, path,
                              evidence={"history_request_id": "trace-many"})
    ids = store.close_request("trace-many", "completed", "都已修改", ["trace-many"], 3)
    assert len(ids) == 2
    assert {store.get_unit(history_id)["path"] for history_id in ids} == {
        "outlines/outline_1.0.0.md", "outlines/outline_1.1.0.md",
    }
    assert all(any(event["kind"] == "user_answer" for event in store.get_unit(history_id)["events"])
               for history_id in ids)


def test_agent_runs_on_same_file_form_separate_history_units(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    store.start_request("root", "session-a", "修改本节")
    path = store.project_dir / "chapters" / "content_1.1.1.md"
    for run_id, changes in (("writer", [("rev-1", None, "初稿"), ("rev-2", "rev-1", "改稿")]),
                            ("polisher", [("rev-3", "rev-2", "润色稿")])):
        request_id = store.run_request_id("root", run_id)
        store.start_request(request_id, "session-a", "修改本节", parent_request_id="root")
        for revision_id, parent_id, body in changes:
            store.record_revision(path, revision_id, parent_id, body, body,
                                  previous_body="改稿" if parent_id else "",
                                  previous_rendered="改稿" if parent_id else "",
                                  evidence={"history_request_id": request_id})
        store.close_request(request_id, "completed", "完成", ["root"])
    ids = store.close_request("root", "completed", "完成", ["root"])
    units = store.list_document_units("chapters/content_1.1.1.md")
    assert [unit["history_id"] for unit in units] == ids
    assert [(unit["base_revision_id"], unit["final_revision_id"]) for unit in units] == [
        ("", "rev-2"), ("rev-2", "rev-3")]
    assert len(units[0]["revisions"]) == 2
    assert units[0]["previous_history_id"] == ""
    assert units[1]["previous_history_id"] == units[0]["history_id"]
    assert store.get_unit(store._unit_id("root", "request"))["kind"] == "request"


def test_no_change_and_unwritten_failure_do_not_advance_file_history(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    path = "chapters/content_1.1.1.md"
    store.start_request("first", "session-a", "写正文")
    store.record_revision(store.project_dir / path, "rev-1", None, "初稿", "初稿",
                          evidence={"history_request_id": "first"})
    first_id = store.close_request("first", "completed", "已写")[0]

    store.start_request("evaluation", "session-a", "需要改吗？")
    store.record_no_change("rev-1", "需要改吗？", "无需修改",
                           request_id="evaluation", path=path)
    evaluation_id = store.close_request("evaluation", "completed", "无需修改")[0]
    assert store.get_unit(evaluation_id)["previous_history_id"] == first_id

    store.start_request("failed", "session-a", "尝试修改")
    store.append_document_event("failed", path, "tool_failure", "修订冲突")
    store.close_request("failed", "completed", "修改失败")
    failed_id = store._unit_id("failed", "document", path)
    assert store.get_unit(failed_id)["previous_history_id"] == first_id

    store.start_request("second", "session-a", "修改正文")
    store.record_revision(store.project_dir / path, "rev-2", "rev-1", "改稿", "改稿",
                          previous_body="初稿", previous_rendered="初稿",
                          evidence={"history_request_id": "second"})
    second_id = store.close_request("second", "completed", "已修改")[0]
    assert store.get_unit(second_id)["previous_history_id"] == first_id


def test_failed_run_that_wrote_a_revision_remains_in_file_chain(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    path = "chapters/content_1.1.1.md"
    for request_id, revision_id, parent_id, status in (
        ("first", "rev-1", None, "completed"),
        ("partial", "rev-2", "rev-1", "failed"),
        ("next", "rev-3", "rev-2", "completed"),
    ):
        store.start_request(request_id, "session-a", request_id)
        store.record_revision(store.project_dir / path, revision_id, parent_id,
                              revision_id, revision_id,
                              previous_body=parent_id or "",
                              previous_rendered=parent_id or "",
                              evidence={"history_request_id": request_id})
        store.close_request(request_id, status, status)
    first = store.get_unit(store._unit_id("first", "document", path))
    partial = store.get_unit(store._unit_id("partial", "document", path))
    next_unit = store.get_unit(store._unit_id("next", "document", path))
    assert partial["previous_history_id"] == first["history_id"]
    assert next_unit["previous_history_id"] == partial["history_id"]


def test_no_change_is_event_without_new_body_version(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    path = store.project_dir / "chapters" / "content_1.1.1.md"
    store.record_revision(path, "rev-1", None, "正文", "正文")
    store.start_request("trace-b", "session-a", "这里有问题吗？")
    decision_id = store.record_no_change(
        "rev-1", "这里有问题吗？", "现有描写已满足目标",
        request_id="trace-b", path="chapters/content_1.1.1.md",
    )
    ids = store.close_request("trace-b", "completed", "无需修改", ["trace-b"], 2)
    unit = store.get_unit(ids[0])
    assert unit["final_revision_id"] == ""
    assert unit["base_revision_id"] == "rev-1"
    assert [e["kind"] for e in unit["events"]].count("evaluation_no_change") == 1
    assert any(e["event_id"] == decision_id for e in unit["events"])
    assert [r["revision_id"] for r in unit["revisions"]] == ["rev-1"]
    assert len(store.list_versions("chapters/content_1.1.1.md")) == 1


def test_failed_attempt_remains_and_next_change_links_to_it(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    store.start_request("trace-failed", "session-a", "修改章节")
    store.append_document_event("trace-failed", "chapters/content_1.1.1.md",
                                "tool_failure", "revision_conflict")
    assert store.close_request("trace-failed", "completed", "失败", ["trace-failed"], 1) == []
    failed_id = store._unit_id("trace-failed", "document", "chapters/content_1.1.1.md")
    assert store.get_unit(failed_id)["status"] == "failed"
    store.start_request("trace-retry", "session-a", "根据最新版本重试")
    path = store.project_dir / "chapters" / "content_1.1.1.md"
    store.record_revision(path, "rev-1", None, "已修改", "已修改",
                          evidence={"history_request_id": "trace-retry"})
    ids = store.close_request("trace-retry", "completed", "完成", ["trace-retry"], 2)
    assert store.get_unit(ids[0])["previous_attempt_id"] == failed_id
    assert {event["content"] for event in store.get_unit(ids[0])["previous_attempt_events"]} == {
        "修改章节", "revision_conflict",
    }


def test_restart_marks_collecting_attempt_interrupted(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    store.start_request("trace-open", "session-a", "修改章节")
    store.append_document_event("trace-open", "chapters/content_1.1.1.md",
                                "reviewer_output", "节奏过快")
    assert store.interrupt_collecting() == 2
    document = store.get_unit(store._unit_id("trace-open", "document", "chapters/content_1.1.1.md"))
    assert document["status"] == "interrupted"
    assert document["events"][0]["content"] == "修改章节"


def test_conversation_analyzer_selects_valid_prior_turns_and_is_idempotent(tmp_path):
    class FakeTraceStore:
        async def list_session_turns(self, _session_id):
            return [{"turn_no": n, "user_content": f"问题{n}",
                     "assistant_content": f"回答{n}", "source_trace_id": f"trace-{n}"}
                    for n in range(1, 5)]

    class FakeLLM:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            payload = ({"ranges": [{"start": 1, "end": 2}, {"start": 50, "end": 60}],
                        "reason": "这两轮讨论了同一设定"} if len(self.calls) == 1 else
                       {"records": [{"layer": "memory", "signal": "strong",
                                     "title": "保留双时间线", "claim": "项目保留双时间线",
                                     "content": "项目保留双时间线", "category": "project",
                                     "domain": "outline", "kind": "project_decision",
                                     "source_event_ids": [self.event_id],
                                     "semantic": {"scenario": "project_decision", "what": "项目保留双时间线"}}]})
            yield type("Chunk", (), {"type": "text_delta", "content": json.dumps(payload, ensure_ascii=False)})()

    store = DocumentHistoryStore(tmp_path / "project-a")
    history_id = store.start_request("trace-4", "session-a", "保留双时间线")
    store.close_request("trace-4", "completed", "好的", ["trace-4"], 4)
    llm = FakeLLM()
    llm.event_id = store.get_unit(history_id)["events"][0]["event_id"]
    analyzer = HistoryAnalyzer(llm, str(tmp_path), FakeTraceStore())
    asyncio.run(analyzer.analyze("project-a", history_id))
    store.set_analysis_status(history_id, "failed", "simulate crash after write")
    asyncio.run(analyzer.analyze("project-a", history_id))
    unit = store.get_unit(history_id)
    assert unit["selected_turns"]["ranges"] == [{"start": 1, "end": 2}]
    assert [row["turn_no"] for row in unit["selected_turns"]["turns"]] == [1, 2]
    assert len(llm.calls) == 2
    memories = FileLifecycleStore(str(tmp_path), "project-a").list("memory")
    assert len(memories) == 1
    assert memories[0]["support_sources"] == [f"history:{history_id}"]
    assert memories[0]["history_ids"] == [history_id]
    assert memories[0]["support_count"] == 1


def test_extracted_source_text_must_occur_in_user_evidence(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    history_id = store.start_request("trace-source", "session-a", "这句太直白：她握紧杯子。")
    store.close_request("trace-source", "completed", "收到", ["trace-source"], 1)
    unit = store.get_unit(history_id)
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    event_id = unit["events"][0]["event_id"]
    analyzer._apply("project-a", unit, {"records": [{
        "layer": "memory", "signal": "strong", "title": "压抑表达",
        "claim": "用动作表现压力", "category": "reference", "kind": "text_feedback",
        "source_event_ids": [event_id], "source_text": "模型编造的原文",
    }]})
    record = FileLifecycleStore(str(tmp_path), "project-a").list("memory")[0]
    assert "source_text" not in record
    assert record["semantic_evidence"][0]["history_excerpts"][0]["content"] == "这句太直白：她握紧杯子。"


def test_revision_markers_cannot_be_memory_sources(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    store.start_request("trace-revision", "session-a", "把语气改得克制")
    path = "chapters/content_1.1.2.md"
    store.record_revision(store.project_dir / path, "rev-1", None, "改稿", "改稿",
                          evidence={"history_request_id": "trace-revision"})
    store.append_document_event("trace-revision", path, "revision_reference",
                                '{"revision_id":"rev-1"}')
    history_id = store.close_request("trace-revision", "completed", "已修改")[0]
    unit = store.get_unit(history_id)
    user_id = next(e["event_id"] for e in unit["events"] if e["kind"] == "user_input")
    revision_ids = [e["event_id"] for e in unit["events"]
                    if e["kind"] in {"revision", "revision_reference"}]
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    view = analyzer._evidence_view(unit)
    assert not {"revision", "revision_reference"} & {e["kind"] for e in view["events"]}
    assert not {"revision", "revision_reference"} & {
        e["type"] for e in analyzer._event_view(unit)}
    analyzer._apply("project-a", unit, {"records": [{
        "layer": "memory", "claim": "修订号不是偏好", "source_event_ids": revision_ids,
    }]})
    assert FileLifecycleStore(str(tmp_path), "project-a").list("memory") == []
    analyzer._apply("project-a", unit, {"records": [{
        "layer": "memory", "claim": "写作语气保持克制",
        "source_event_ids": [*revision_ids, user_id],
    }]})
    record = FileLifecycleStore(str(tmp_path), "project-a").list("memory")[0]
    assert record["history_event_ids"] == [user_id]
    assert record["semantic_evidence"][0]["history_excerpts"][0]["kind"] == "user_input"


def test_two_findings_in_one_history_add_only_one_independent_support(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project-a")
    history_id = store.start_request("trace-support", "session-a", "以后场景距离要明确")
    store.close_request("trace-support", "completed", "收到", ["trace-support"], 1)
    unit = store.get_unit(history_id)
    event_id = unit["events"][0]["event_id"]
    files = FileLifecycleStore(str(tmp_path), "project-a")
    existing = files.write("memory", {
        "title": "明确距离", "claim": "场景距离要明确", "content": "场景距离要明确",
        "category": "project", "domain": "writing", "weight": 65, "support_count": 1,
        "support_sources": ["trace:old"],
    })
    candidate = {"layer": "memory", "signal": "strong", "title": "明确距离",
                 "claim": "场景距离要明确", "category": "project", "domain": "writing",
                 "source_event_ids": [event_id], "relation": "support",
                 "related_layer": "memory", "related_id": existing["id"]}
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    analyzer._apply("project-a", unit, {"records": [candidate, candidate]})
    analyzer._apply("project-a", unit, {"records": [candidate, candidate]})
    merged = files.get("memory", existing["id"])
    assert merged["support_count"] == 2
    assert merged["support_sources"] == ["trace:old", f"history:{history_id}"]
    assert {row["record_index"] for row in merged["semantic_evidence"]} == {0, 1}
