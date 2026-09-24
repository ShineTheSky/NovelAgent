import asyncio
import json
from types import SimpleNamespace

from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.core.review_workflow import ReviewPolishWorkflow
from novelagent.tools.base import ToolContext
from novelagent.tools.get_trace_context import GetTraceContextTool


def _trajectory_response(kwargs):
    prompt = kwargs["messages"][-1]["content"]
    event = json.loads(prompt.split("Raw trace events:", 1)[1])[0]
    trace_id = event["trace_id"]
    turn_no = int(event["turn"])
    return json.dumps({
        "trajectory": [{
            "role": "observation",
            "content": json.dumps(event, ensure_ascii=False),
            "timestamp": event["timestamp"],
        }],
        "provenance": [{
            "record_index": 0,
            "trace_id": trace_id,
            "turn_id": f"{trace_id}:{turn_no}",
            "turn_no": turn_no,
            "source_event_ids": [event["event_id"]],
        }],
    }, ensure_ascii=False)


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


def test_legacy_evidence_directory_is_migrated_to_insight(tmp_path):
    legacy = tmp_path / "project-a" / ".evidence" / "project"
    legacy.mkdir(parents=True)
    (legacy / "evi_legacy.md").write_text(
        "---\nid: evi_legacy\nclaim: 旧洞察\ncategory: project\ndomain: writing\n"
        "weight: 15\nupdated: '2026-09-01T00:00:00+00:00'\n---\n\n旧洞察正文\n",
        encoding="utf-8",
    )

    records = FileLifecycleStore(str(tmp_path), "project-a").list("insight")

    assert len(records) == 1
    assert records[0]["id"] == "evi_legacy"
    assert records[0]["layer"] == "insight"
    assert records[0]["file_path"] == ".insight/project/evi_legacy.md"
    assert not (tmp_path / "project-a" / ".evidence").exists()


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
        assert limit == 500
        return [{
            "event_id": "event-b", "trace_id": "trace-b", "sequence_no": 1, "event_type": "assistant_turn",
            "payload": {"turn": 7, "content": "同一 Session 的较早回复。"},
        }]

    async def get_trace_turn_range(self, trace_id, start_turn, end_turn, limit):
        assert (trace_id, start_turn, end_turn, limit) == ("trace-b", 6, 8, 300)
        return [{
            "event_id": "event-b", "trace_id": "trace-b", "sequence_no": 1, "event_type": "assistant_turn",
            "payload": {"turn": 7, "content": "按 turn 区间读取。"},
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


def test_cached_trace_analyzer_executes_trace_context_instead_of_rejecting_it(tmp_path):
    llm = _TraceContextLLM()
    tool = GetTraceContextTool(_TraceContextStore(), "session-a", {"trace-a": ["event-a"]})
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), _TraceContextStore())

    result = asyncio.run(analyzer._extract(
        "分析 Trace", [{"role": "user", "content": "继承的主会话上下文"}],
        trace_tool=tool, project_id="project-a", source_trace_id="trace-current",
        session_id="session-a",
        cached_tools=[{"name": "Read", "description": "读取文件", "parameters": {"type": "object"}}],
    ))

    assert json.loads(result) == {"items": [], "records": []}
    assert len(llm.calls) == 2
    assert [schema["name"] for schema in llm.calls[0]["tools"]] == ["Read", "GetTraceContext"]
    assert llm.calls[1]["tools"] is None
    tool_result = next(message for message in llm.calls[1]["messages"] if message["role"] == "tool_result")
    assert "这句话太直白" in tool_result["content"]


class _ReadContextLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tools"]:
            yield SimpleNamespace(
                type="tool_use", tool_name="Read", tool_call_id="read-call",
                tool_input={"path": "outline.md"},
            )
        else:
            yield SimpleNamespace(type="text_delta", content='{"items": [], "records": []}')


def test_cached_trace_analyzer_allows_read_inside_current_project(tmp_path):
    project_dir = tmp_path / "project-a"
    project_dir.mkdir()
    (project_dir / "outline.md").write_text("当前卷纲修订内容", encoding="utf-8")
    llm = _ReadContextLLM()
    trace_tool = GetTraceContextTool(_TraceContextStore(), "session-a")
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), _TraceContextStore())

    result = asyncio.run(analyzer._extract(
        "分析 Trace", [{"role": "user", "content": "继承的主会话上下文"}],
        trace_tool=trace_tool, project_id="project-a", source_trace_id="trace-current",
        session_id="session-a",
        cached_tools=[{"name": "Read", "description": "读取文件", "parameters": {"type": "object"}}],
    ))

    assert json.loads(result) == {"items": [], "records": []}
    assert [schema["name"] for schema in llm.calls[0]["tools"]] == ["Read", "GetTraceContext"]
    tool_result = next(message for message in llm.calls[1]["messages"] if message["role"] == "tool_result")
    assert "当前卷纲修订内容" in tool_result["content"]


def test_trace_context_tool_allows_any_trace_in_current_session():
    tool = GetTraceContextTool(_TraceContextStore(), "session-a", {"trace-a": ["event-a"]})
    context = ToolContext(session_id="session-a", project_id="project-a", working_dir=".")

    result = asyncio.run(tool.execute({"trace_ids": ["trace-b"]}, context))

    assert result.success
    assert result.data["traces"][0]["trace_id"] == "trace-b"
    assert "同一 Session" in result.data["traces"][0]["events"][0]["content"]


def test_trace_context_tool_reads_requested_turn_range():
    tool = GetTraceContextTool(_TraceContextStore(), "session-a")
    context = ToolContext(session_id="session-a", project_id="project-a", working_dir=".")

    result = asyncio.run(tool.execute({
        "requests": [{"trace_id": "trace-b", "start_turn": 6, "end_turn": 8}],
    }, context))

    assert result.success
    assert result.data["traces"][0]["turn_range"] == {"start": 7, "end": 7}
    assert result.data["traces"][0]["events"][0]["turn"] == 7
    assert "按 turn 区间读取" in result.data["traces"][0]["events"][0]["content"]


def test_trace_context_tool_rejects_trace_from_another_session():
    tool = GetTraceContextTool(_TraceContextStore(), "session-a")
    context = ToolContext(session_id="session-a", project_id="project-a", working_dir=".")

    result = asyncio.run(tool.execute({"trace_ids": ["trace-other"]}, context))

    assert not result.success
    assert "不属于当前 Session" in result.error


def test_local_analysis_keeps_five_turns_around_checkpoint_and_infers_answer_turn():
    events = []
    sequence = 0
    for turn in range(1, 13):
        sequence += 1
        events.append({
            "event_id": f"llm-{turn}", "trace_id": "trace-a", "sequence_no": sequence,
            "event_type": "assistant_turn", "payload": {"turn": turn, "content": f"turn {turn}"},
        })
        if turn == 8:
            sequence += 1
            events.append({
                "event_id": "answer-8", "trace_id": "trace-a", "sequence_no": sequence,
                "event_type": "user_answer", "payload": {"answers": {"setting": "保留旧球鞋"}},
            })
            sequence += 1
            events.append({
                "event_id": "checkpoint-8", "trace_id": "trace-a", "sequence_no": sequence,
                "event_type": "tool_call", "payload": {
                    "turn": 8, "tool": "CreateTraceCheckpoint", "params": {"reason": "关键设定"},
                },
            })

    selected = FileTraceAnalyzer._select_analysis_events(events)
    turns = {event["trace_turn"] for event in selected}
    refs = FileTraceAnalyzer._trace_refs(selected, ["answer-8", "checkpoint-8"], "trace-a")

    assert turns == {6, 7, 8, 9, 10}
    assert refs == [{
        "trace_id": "trace-a", "turn": 8,
        "source_event_ids": ["answer-8", "checkpoint-8"],
    }]


def test_cached_branch_source_excerpt_maps_back_to_trace_turn():
    events = FileTraceAnalyzer._select_analysis_events([
        {
            "event_id": "user-4", "trace_id": "trace-a", "sequence_no": 1,
            "event_type": "user_message", "payload": {"turn": 4, "content": "以后动作场景必须写清人物距离。"},
        },
        {
            "event_id": "assistant-4", "trace_id": "trace-a", "sequence_no": 2,
            "event_type": "assistant_turn", "payload": {"turn": 4, "content": "明白。"},
        },
    ])

    event_id = FileTraceAnalyzer._match_source_event_id(events, "动作场景必须写清人物距离")
    refs = FileTraceAnalyzer._trace_refs(events, [event_id], "trace-a")

    assert event_id == "user-4"
    assert refs == [{"trace_id": "trace-a", "turn": 4, "source_event_ids": ["user-4"]}]


class _FallbackTraceStore:
    def __init__(self):
        self.statuses = []

    async def get_trace(self, trace_id):
        return {"trace_id": trace_id, "session_id": "session-a", "analysis_status": "pending"}

    async def list_events(self, trace_id, limit):
        return [{
            "event_id": "event-user", "trace_id": trace_id, "sequence_no": 1,
            "event_type": "user_message", "payload": {"turn": 1, "content": "保留这个设定。"},
        }]

    async def save_trace_classification(self, trace_id, items):
        pass

    async def set_trace_analysis_status(self, trace_id, status):
        self.statuses.append((trace_id, status))


class _OverflowThenLocalLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tag"] == ":trace-fork/trajectory" and kwargs["position"] == "main_loop":
            yield SimpleNamespace(type="error", error="maximum context length exceeded")
        elif kwargs["tag"] == ":trace-fork/trajectory/fallback":
            yield SimpleNamespace(type="text_delta", content=_trajectory_response(kwargs))
        else:
            yield SimpleNamespace(type="text_delta", content='{"items": [], "records": []}')


def test_trace_analysis_falls_back_to_local_turns_when_cached_branch_overflows(tmp_path):
    llm = _OverflowThenLocalLLM()
    store = _FallbackTraceStore()
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), store)

    asyncio.run(analyzer.analyze(
        "trace-a", "project-a", branch_messages=[{"role": "user", "content": "主会话前缀"}],
    ))

    assert [call["position"] for call in llm.calls] == [
        "main_loop", "memory_summary_fallback", "memory_summary_fallback",
    ]
    assert llm.calls[0]["messages"][0]["content"] == "主会话前缀"
    assert "Raw trace events:" in llm.calls[0]["messages"][-1]["content"]
    assert llm.calls[2]["messages"][0]["content"].startswith("[Memory/Summary Agent：轨迹整理轮]")
    assert "normalized trajectory" in llm.calls[2]["messages"][-1]["content"]
    assert store.statuses == [("trace-a", "complete")]


class _InvalidThenLocalLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tag"] == ":trace-fork/trajectory" and kwargs["position"] == "main_loop":
            yield SimpleNamespace(type="text_delta", content="不是 JSON")
        elif kwargs["tag"] == ":trace-fork/trajectory/fallback":
            yield SimpleNamespace(type="text_delta", content=_trajectory_response(kwargs))
        else:
            yield SimpleNamespace(type="text_delta", content='{"items": [], "records": []}')


def test_trace_analysis_falls_back_when_cached_branch_returns_invalid_json(tmp_path):
    llm = _InvalidThenLocalLLM()
    store = _FallbackTraceStore()
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), store)

    asyncio.run(analyzer.analyze(
        "trace-a", "project-a", branch_messages=[{"role": "user", "content": "主会话前缀"}],
    ))

    assert [call["position"] for call in llm.calls] == [
        "main_loop", "memory_summary_fallback", "memory_summary_fallback",
    ]
    assert store.statuses == [("trace-a", "complete")]


class _TwoStageMergeLLM:
    def __init__(self, related_id):
        self.related_id = related_id
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tag"] == ":trace-fork/trajectory/fallback":
            yield SimpleNamespace(type="text_delta", content=_trajectory_response(kwargs))
            return
        if kwargs["tag"] == ":trace-fork/reconcile":
            yield SimpleNamespace(type="text_delta", content=json.dumps({"decisions": [{
                "candidate_index": 0,
                "relation": "support",
                "related_id": self.related_id,
                "related_layer": "insight",
                "reason": "同一条写作要求",
            }]}, ensure_ascii=False))
            return
        yield SimpleNamespace(type="text_delta", content=json.dumps({
            "window_summary": "当前窗口摘要",
            "items": [{
                "event_id": "event-user", "type": "feedback",
                "summary": "动作距离需要明确", "confidence": 0.9,
            }],
            "records": [{
                "layer": "insight", "category": "project", "domain": "writing",
                "title": "动作场景写清距离", "claim": "动作场景写清距离",
                "content": "动作场景需要明确人物距离。",
                "source_event_ids": ["event-user"], "signal": "weak",
                "relation": "new", "confidence": 0.9,
            }],
        }, ensure_ascii=False))


def test_second_stage_reconciles_new_candidate_into_existing_insight(tmp_path):
    files = FileLifecycleStore(str(tmp_path), "project-a")
    existing = files.write("insight", {
        "title": "动作场景写清距离", "claim": "动作场景写清距离",
        "content": "动作场景需要明确人物距离。",
        "category": "project", "domain": "writing",
        "trace_id": "trace-old", "trace_ids": ["trace-old"],
        "source_event_ids": ["event-old"], "weight": 15, "support_count": 1,
    })
    llm = _TwoStageMergeLLM(existing["id"])
    store = _FallbackTraceStore()
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), store)

    asyncio.run(analyzer.analyze("trace-a", "project-a"))

    insights = files.list("insight")
    assert len(insights) == 1
    assert insights[0]["id"] == existing["id"]
    assert insights[0]["support_count"] == 2
    assert insights[0]["weight"] == 30
    assert [call["tag"] for call in llm.calls] == [
        ":trace-fork/trajectory/fallback", ":trace-fork", ":trace-fork/reconcile",
    ]


class _SemanticExtractionLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["tag"] == ":trace-fork/trajectory/fallback":
            yield SimpleNamespace(type="text_delta", content=_trajectory_response(kwargs))
            return
        if kwargs["tag"] == ":trace-fork/reconcile":
            yield SimpleNamespace(type="text_delta", content=json.dumps({"decisions": [{
                "candidate_index": 0, "relation": "new", "reason": "新的明确规则",
            }]}, ensure_ascii=False))
            return
        yield SimpleNamespace(type="text_delta", content=json.dumps({
            "window_summary": "用户确认动作场景规则。",
            "items": [{
                "event_id": "event-user", "type": "correction",
                "summary": "动作场景必须写清人物距离", "confidence": 0.95,
            }],
            "records": [{
                "layer": "memory", "category": "user", "domain": "writing",
                "title": "动作场景写清人物距离", "claim": "动作场景写清人物距离",
                "content": "动作场景必须写清人物距离。",
                "source_event_ids": ["event-user"], "signal": "strong",
                "relation": "new", "confidence": 0.95,
                "semantic": {
                    "scenario": "user_directive",
                    "what": "用户要求动作场景必须写清人物距离。",
                    "who": ["用户"], "when": "", "where": "动作场景", "why": "",
                    "entities": [{"name": "动作场景", "type": "concept", "role": "适用对象"}],
                    "relations": [{"type": "applies_to", "target": "动作场景", "description": "明确限定"}],
                    "attributes": {"target": "动作场景", "desired": "人物距离清楚"},
                },
            }],
        }, ensure_ascii=False))


def test_trace_analysis_routes_scenarios_and_persists_semantic_metadata(tmp_path):
    llm = _SemanticExtractionLLM()
    store = _FallbackTraceStore()
    analyzer = FileTraceAnalyzer(llm, str(tmp_path), store)

    asyncio.run(analyzer.analyze("trace-a", "project-a"))

    extraction_call = next(call for call in llm.calls if call["tag"] == ":trace-fork")
    extraction_prompt = extraction_call["messages"][-1]["content"]
    assert 'scenario="user_directive"' in extraction_prompt
    assert 'scenario="story_knowledge"' in extraction_prompt

    memories = FileLifecycleStore(str(tmp_path), "project-a").list("memory")
    assert len(memories) == 1
    assert memories[0]["extraction_profile"] == "user_directive"
    assert memories[0]["semantic"]["attributes"] == {
        "target": "动作场景", "desired": "人物距离清楚",
    }
    assert memories[0]["semantic"]["relations"][0]["type"] == "applies_to"
