import asyncio
import json
from types import SimpleNamespace

from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.core.review_workflow import ReviewPolishWorkflow
from novelagent.tools.base import ToolContext
from novelagent.tools.get_trace_context import GetTraceContextTool


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


def test_review_issue_keeps_writing_domain_and_kind_when_promoted(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    memory = store.write("memory", {
        "title": "对话连续缺少动作支点",
        "claim": "连续对话需要动作或空间变化作为支点。",
        "content": "出现三轮以上连续对白时，检查是否缺少动作、视线或位置变化。",
        "category": "project",
        "domain": "writing",
        "kind": "review_issue",
        "weight": 220,
        "support_count": 4,
    })

    pattern = store.promote_memory_to_pattern(memory, ["trace-a"])

    assert pattern["category"] == "project"
    assert pattern["domain"] == "writing"
    assert pattern["kind"] == "review_issue"


def test_review_issue_context_is_injected_for_future_writing_and_review(tmp_path):
    store = FileLifecycleStore(str(tmp_path), "project-a")
    store.write("memory", {
        "title": "场景空间关系容易丢失",
        "claim": "动作场景需要持续标记相对位置。",
        "content": "多人移动后检查站位、距离与视线是否仍可追踪。",
        "category": "project",
        "domain": "writing",
        "kind": "review_issue",
        "weight": 60,
        "support_count": 4,
    })
    workflow = ReviewPolishWorkflow(None, str(tmp_path))

    context = workflow._memory_context("project-a")

    assert "高频写作错误" in context
    assert "场景空间关系容易丢失" in context
    assert "累计 4 次" in context


def test_reviewer_result_is_visible_to_trace_memory_extraction():
    summary = FileTraceAnalyzer._event_summary({
        "event_id": "event-review",
        "trace_id": "trace-a",
        "event_type": "workflow_subagent_done",
        "payload": {
            "preset": "reviewer",
            "workflow": "auto_review",
            "run_id": "run-review",
            "result": "问题：连续对白缺少动作支点。证据：三轮对白中人物位置没有变化。",
        },
    })

    assert summary["preset"] == "reviewer"
    assert summary["workflow"] == "auto_review"
    assert "连续对白缺少动作支点" in summary["result"]


def test_legacy_main_agent_reviewer_summary_is_treated_as_auto_review():
    summary = FileTraceAnalyzer._event_summary({
        "event_id": "event-legacy-review",
        "trace_id": "trace-a",
        "event_type": "assistant_turn",
        "payload": {
            "content": "**审阅结论（reviewer）**\n现实侧信息堆砌，需要压缩。",
        },
    })

    assert summary["preset"] == "reviewer"
    assert summary["workflow"] == "auto_review"
    assert summary["legacy_summary"] is True


def test_checkpoint_reason_is_visible_to_trace_memory_extraction():
    event = {
        "event_id": "event-checkpoint",
        "trace_id": "trace-a",
        "event_type": "tool_call",
        "payload": {
            "tool": "CreateTraceCheckpoint",
            "params": {"reason": "用户指出具体正文描写不够生动。"},
        },
    }

    summary = FileTraceAnalyzer._event_summary(event)
    reasons = FileTraceAnalyzer._checkpoint_reasons([event])

    assert summary["params"]["reason"] == "用户指出具体正文描写不够生动。"
    assert reasons == [{"event_id": "event-checkpoint", "reason": "用户指出具体正文描写不够生动。"}]


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


class _TraceContextStore:
    async def get_project_trace(self, project_id, trace_id):
        sessions = {"trace-a": "session-a", "trace-b": "session-a", "trace-other": "session-b"}
        if project_id == "project-a" and trace_id in sessions:
            return {"trace_id": trace_id, "project_id": project_id, "session_id": sessions[trace_id]}
        return None

    async def get_trace_event_window(self, trace_id, event_ids, before, after):
        assert trace_id == "trace-a"
        assert event_ids == ["event-a"]
        assert (before, after) == (2, 2)
        return [{
            "event_id": "event-a", "trace_id": "trace-a", "event_type": "user_message",
            "payload": {"content": "这句话太直白，压着一点写。"},
        }]

    async def list_events(self, trace_id, limit):
        assert trace_id == "trace-b"
        assert limit == 40
        return [{
            "event_id": "event-b", "trace_id": "trace-b", "event_type": "assistant_turn",
            "payload": {"content": "同一 Session 的较早回复。"},
        }]


class _TraceContextLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tools"]:
            yield SimpleNamespace(
                type="tool_use", tool_name="GetTraceContext", tool_call_id="trace-call",
                tool_input={"trace_ids": ["trace-a"]},
            )
        else:
            yield SimpleNamespace(type="text_delta", content='{"items": [], "records": []}')


def test_trace_analyzer_uses_real_tool_for_bounded_context_lookup(tmp_path):
    llm = _TraceContextLLM()
    tool = GetTraceContextTool(_TraceContextStore(), "session-a", {"trace-a": ["event-a"]})
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), _TraceContextStore())

    result = asyncio.run(analyzer._extract(
        "分析 Trace", None, trace_tool=tool, project_id="project-a", source_trace_id="trace-current",
        session_id="session-a",
    ))

    assert json.loads(result) == {"items": [], "records": []}
    assert len(llm.calls) == 2
    assert llm.calls[0]["tools"][0]["name"] == "GetTraceContext"
    assert llm.calls[1]["tools"] is None
    tool_result = next(message for message in llm.calls[1]["messages"] if message["role"] == "tool_result")
    assert "这句话太直白" in tool_result["content"]


def test_trace_context_tool_allows_any_trace_in_current_session():
    tool = GetTraceContextTool(_TraceContextStore(), "session-a", {"trace-a": ["event-a"]})
    context = ToolContext(session_id="session-a", project_id="project-a", working_dir=".")

    result = asyncio.run(tool.execute({"trace_ids": ["trace-b"]}, context))

    assert result.success
    assert result.data["traces"][0]["trace_id"] == "trace-b"
    assert "同一 Session" in result.data["traces"][0]["events"][0]["content"]


def test_trace_context_tool_rejects_trace_from_another_session():
    tool = GetTraceContextTool(_TraceContextStore(), "session-a")
    context = ToolContext(session_id="session-a", project_id="project-a", working_dir=".")

    result = asyncio.run(tool.execute({"trace_ids": ["trace-other"]}, context))

    assert not result.success
    assert "不属于当前 Session" in result.error
