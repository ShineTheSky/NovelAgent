import asyncio
import json
from types import SimpleNamespace

from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore


def _feedback(**overrides):
    item = {
        "title": "减少直白解释",
        "claim": "减少直白解释",
        "content": "优先用动作和停顿表达人物压力。",
        "category": "reference",
        "kind": "text_feedback",
        "domain": "writing",
        "artifact_path": "chapters/content_1.1.1.md",
        "artifact_revision_id": "rev_a",
        "anchor_excerpt": "林深没有回答，只是握紧了杯子。",
        "anchor_sha256": "anchor-a",
        "feedback_direction": "减少直白解释，增加动作留白。",
        "weight": 65,
        "support_count": 1,
        "trace_ids": ["trace_a"],
        "source_event_ids": ["event_a"],
        "feedback_history": [{"trace_id": "trace_a", "source_event_ids": ["event_a"], "relation": "new"}],
        "user_inputs": [{"trace_id": "trace_a", "event_id": "event_a", "content": "这句话太直白，压着一点写。"}],
        "feedback_count": 1,
    }
    item.update(overrides)
    return item


def test_same_anchor_feedback_refreshes_without_weight(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    original = store.write("memory", _feedback())

    refreshed = store.merge_text_feedback(original, _feedback(
        title="补充压抑感",
        content="减少解释，并让动作停顿持续更久。",
        feedback_direction="减少解释，延长动作停顿。",
        user_requirements="减少直白解释，但保留人物承压时的克制感。",
        revision_direction="压缩解释，通过更长的动作停顿表现压力。",
    ), "trace_b", ["event_b"], [{"trace_id": "trace_b", "event_id": "event_b", "content": "这句还是太直白，压着一点写。"}])
    saved = store.write("memory", refreshed)

    assert saved["weight"] == 65
    assert saved["support_count"] == 1
    assert saved["feedback_count"] == 2
    assert saved["user_requirements"] == "减少直白解释，但保留人物承压时的克制感。"
    assert saved["revision_direction"] == "压缩解释，通过更长的动作停顿表现压力。"
    assert "## 用户要求" in saved["content"]
    assert "## 修改方向" in saved["content"]
    assert "这句话太直白，压着一点写。" in saved["content"]
    assert "这句还是太直白，压着一点写。" in saved["content"]


def test_manual_downgrade_blocks_auto_promotion_until_reconfirmed(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    pattern = store.write("pattern", _feedback(weight=240, support_count=4))

    downgraded = store.downgrade("pattern", pattern["id"])

    assert downgraded["layer"] == "memory"
    assert downgraded["promotion_status"] == "manual_review"
    assert not store.can_auto_promote(downgraded)
    assert "用户认为该记录目前不应保持原有等级" in downgraded["downgrade_reason"]
    assert "## 用户降级备注" in downgraded["content"]

    reconfirmed = store.cancel_manual_review("memory", downgraded["id"])
    assert reconfirmed["layer"] == "memory"
    assert reconfirmed["promotion_status"] == "auto"
    assert reconfirmed["explicitly_reconfirmed_at"]
    assert reconfirmed["manual_review_cancelled_at"]
    assert "downgrade_reason" not in reconfirmed
    assert "downgraded_from" not in reconfirmed
    assert "用户降级备注" not in reconfirmed["content"]
    assert store.can_auto_promote(reconfirmed)
    assert store.get("memory", downgraded["id"])["promotion_status"] == "auto"


def test_same_memory_only_creates_one_pattern(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    memory = store.write("memory", _feedback(weight=220, support_count=3))

    first = store.promote_memory_to_pattern(memory, ["trace_a"])
    memory["weight"] = 250
    memory["support_count"] = 4
    second = store.promote_memory_to_pattern(memory, ["trace_b"])

    patterns = store.list("pattern")
    assert len(patterns) == 1
    assert second["id"] == first["id"]
    assert second["memory_ids"] == [memory["id"]]
    assert second["trace_ids"] == ["trace_a", "trace_b"]
    assert second["weight"] == 250
    assert second["support_count"] == 4


def test_feedback_keeps_multiple_text_anchors_for_targeted_reading(tmp_path):



    store = FileLifecycleStore(str(tmp_path), "project-a")
    original = store.write("memory", _feedback())
    merged = store.merge_text_feedback(original, _feedback(
        artifact_path="chapters/content_1.1.2.md",
        artifact_revision_id="rev_b",
        anchor_excerpt="他把话咽了回去，窗外只剩雨声。",
        anchor_sha256="anchor-b",
    ), "trace_b", ["event_b"], [{"trace_id": "trace_b", "event_id": "event_b", "content": "另一处也不要直说。"}])
    store.write("memory", merged)

    matches = store.text_feedback_for_artifact("chapters/content_1.1.1.md", "林深没有回答，只是握紧了杯子。")
    assert len(matches) == 1
    assert {anchor["artifact_path"] for anchor in matches[0]["anchor_examples"]} == {
        "chapters/content_1.1.1.md", "chapters/content_1.1.2.md",
    }


class _RagReviewLLM:
    def __init__(self, final):
        self.final = final
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tools"]:
            yield SimpleNamespace(
                type="tool_use", tool_name="SearchRag", tool_call_id="rag-call",
                tool_input={"query": "克制表达人物压力", "limit": 5},
            )
        else:
            yield SimpleNamespace(type="text_delta", content=json.dumps(self.final, ensure_ascii=False))


class _RagStore:
    async def search(self, query, limit):
        assert query == "克制表达人物压力"
        assert limit == 3
        return [{"title": "参考片段", "content": "他把话咽了回去。"}]


def test_reference_feedback_only_gets_a_single_rag_review_round(tmp_path):
    candidate = {"records": [{"category": "reference", "kind": "text_feedback", "title": "减少直白解释"}]}
    final = {"records": [{**candidate["records"][0], "feedback_direction": "以动作和停顿代替解释。"}]}
    llm = _RagReviewLLM(final)
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), None, rag_store=_RagStore())

    result = asyncio.run(analyzer._review_reference_feedback(
        "trace-a", "project-a", "初步归纳", None, candidate,
    ))

    assert result == final
    assert len(llm.calls) == 2
    assert llm.calls[0]["tools"][0]["name"] == "SearchRag"
    assert llm.calls[1]["tools"] is None
    tool_result = next(message for message in llm.calls[1]["messages"] if message["role"] == "tool_result")
    assert "参考片段" in tool_result["content"]


def test_only_reference_text_feedback_enters_rag_review():
    assert not FileTraceAnalyzer._has_reference_feedback({"records": [
        {"category": "project", "kind": "text_feedback"},
        {"category": "reference", "kind": "other"},
    ]})
