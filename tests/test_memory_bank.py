from novelagent.trace.memory_bank import MemoryBankStore


def _event(event_id, trace_id, event_type, payload, turn=1, actor="main_agent"):
    return {
        "event_id": event_id,
        "trace_id": trace_id,
        "event_type": event_type,
        "trace_turn": turn,
        "actor": actor,
        "payload": payload,
    }


def test_bank_retains_revision_content_and_links_revision_chain(tmp_path):
    bank = MemoryBankStore(str(tmp_path), "project-a")
    first_events = [
        _event("start-1", "trace-write", "subagent_subagent_start", {
            "run_id": "run-1", "preset": "chapter_writer",
            "task_prompt": "续写第三章，保持林深的克制。",
        }),
        _event("done-1", "trace-write", "subagent_subagent_done", {
            "run_id": "run-1", "preset": "chapter_writer",
            "agent_trace_id": "trace-writer-agent",
            "revision_events": [{
                "path": "chapters/chapter_003.md", "revision_id": "rev-2",
                "parent_revision_id": "rev-1", "source_revision_id": "rev-1",
                "anchor_excerpt": "林深说他很紧张。",
                "anchor_sha256": "before-sha",
                "replacement_excerpt": "林深的指节抵住杯沿。",
                "updated_by": "chapter_writer",
            }],
        }, turn=3, actor="subagent:chapter_writer"),
    ]

    first = bank.retain_artifact_revisions(first_events)
    duplicate = bank.retain_artifact_revisions(first_events)

    assert len(first) == len(duplicate) == 1
    assert len(bank.list()) == 1
    item = first[0]
    assert item["task_prompt"] == "续写第三章，保持林深的克制。"
    assert item["artifact"]["anchor_excerpt"] == "林深说他很紧张。"
    assert item["artifact"]["replacement_excerpt"] == "林深的指节抵住杯沿。"
    assert item["source_agent_trace_id"] == "trace-writer-agent"
    assert item["source_trace_refs"] == [{
        "trace_id": "trace-write", "event_id": "done-1", "turn": 3,
    }]

    second = bank.retain_artifact_revisions([
        _event("done-2", "trace-polish", "workflow_subagent_done", {
            "run_id": "run-2", "preset": "chapter_polisher", "workflow": "auto_polish",
            "revision_events": [{
                "path": "chapters/chapter_003.md", "revision_id": "rev-3",
                "parent_revision_id": "rev-2", "source_revision_id": "rev-2",
                "anchor_excerpt": "林深的指节抵住杯沿。",
                "replacement_excerpt": "林深的指节压着杯沿，久久没有松开。",
                "updated_by": "chapter_polisher",
            }],
        }, actor="workflow:auto_polish"),
    ])[0]

    assert second["related_bank_item_ids"] == [item["bank_item_id"]]


def test_later_feedback_finds_cross_trace_artifact_evidence(tmp_path):
    bank = MemoryBankStore(str(tmp_path), "project-a")
    revision = bank.retain_artifact_revisions([
        _event("start-1", "trace-write", "subagent_subagent_start", {
            "run_id": "run-1", "preset": "chapter_writer",
            "task_prompt": "写出压抑感，不要直接解释情绪。",
        }),
        _event("done-1", "trace-write", "subagent_subagent_done", {
            "run_id": "run-1", "preset": "chapter_writer",
            "revision_events": [{
                "path": "chapters/chapter_003.md", "revision_id": "rev-2",
                "source_revision_id": "rev-1", "anchor_excerpt": "旧段落",
                "replacement_excerpt": "林深说他非常紧张和害怕。",
                "updated_by": "chapter_writer",
            }],
        }),
    ])[0]

    related = bank.related([
        _event("feedback-1", "trace-feedback", "user_message", {
            "content": "林深说他非常紧张和害怕。这段还是太直白了。",
        }),
    ])

    assert [item["bank_item_id"] for item in related] == [revision["bank_item_id"]]
    assert related[0]["source_trace_refs"][0]["trace_id"] == "trace-write"


def test_bank_only_adds_semantic_items_for_extractor_records(tmp_path):
    bank = MemoryBankStore(str(tmp_path), "project-a")
    evidence = bank.write({
        "bank_item_id": "bank-revision", "item_type": "artifact_revision",
        "content": "writer 修改了章节。", "artifact": {"path": "chapters/chapter_003.md"},
    })
    events = [_event("feedback-1", "trace-feedback", "user_message", {
        "content": "这段太直白，要用动作表现压力。",
    })]
    record = {
        "operation_id": "window-1:0", "category": "reference", "kind": "text_feedback",
        "title": "避免直白解释情绪", "claim": "避免直白解释情绪",
        "content": "用户要求以动作表现压力，避免直接解释情绪。",
        "source_event_ids": ["feedback-1"],
        "source_bank_item_ids": [evidence["bank_item_id"], "invented-id"],
        "artifact_path": "chapters/chapter_003.md",
    }

    saved = bank.retain_semantic_records(
        [record], events, "trace-feedback", related_bank_items=[evidence],
    )
    bank.retain_semantic_records([], events, "trace-feedback", related_bank_items=[evidence])

    assert len(saved) == 1
    assert saved[0]["related_bank_item_ids"] == [evidence["bank_item_id"]]
    assert saved[0]["user_inputs"] == ["这段太直白，要用动作表现压力。"]
    assert record["bank_item_ids"] == [saved[0]["bank_item_id"], evidence["bank_item_id"]]
    assert len(bank.list()) == 2


def test_bank_skips_routine_and_non_story_artifacts(tmp_path):
    bank = MemoryBankStore(str(tmp_path), "project-a")
    events = [
        _event("read-1", "trace-a", "tool_result", {
            "tool": "Read", "success": True, "data": "读取完成",
        }),
        _event("write-1", "trace-a", "tool_result", {
            "tool": "Write", "success": True,
            "revision_events": [{
                "path": "debug/trace.md", "revision_id": "rev-debug",
                "anchor_excerpt": "before", "replacement_excerpt": "after",
            }],
        }),
    ]

    assert bank.retain_artifact_revisions(events) == []
    assert bank.list() == []
