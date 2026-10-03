from copy import deepcopy
import json

import pytest

from novelagent.core.llm_turn import query
from novelagent.core.review_workflow import ReviewPolishWorkflow
from novelagent.core.session import Session
from novelagent.core.subagent import SubAgentRunner
from novelagent.history import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.history.sources import root_history_ids
from novelagent.llm.client import LLMResponse
from novelagent.tools.registry import ToolRegistry
from novelagent.tools.write import WriteTool
from novelagent.trace.file_lifecycle import FileLifecycleStore


PATH = "chapters/content_1.1.1.md"
REPORT = "## 用户需求验收\n克制语气：已落实，证据是握杯动作。\n## 常规写作检查\n退出条件与广播冲突，建议统一规则口径。"


class Model:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        for chunk in self.turns.pop(0):
            yield chunk


@pytest.mark.asyncio
async def test_query_snapshot_contains_actual_tool_results_and_final_response():
    model = Model([
        [LLMResponse(type="thinking", content="采用动作表达"),
         LLMResponse(type="tool_use", tool_name="Write", tool_call_id="write", tool_input={"content": "正文"}), LLMResponse(type="done")],
        [LLMResponse(type="text_delta", content="已写入"), LLMResponse(type="done")],
    ])
    snapshot = []
    initial = [{"role": "user", "content": "写作需求"}]

    async def execute(*_args):
        return True, "实际写入结果", ""

    events = [e async for e in query(model, position="sub_agent", system_prompt="写作角色",
        messages=initial, tools=None, max_turns=1, tag="test", execute_tool=execute, context_snapshot=snapshot)]
    assert events[-1] == {"type": "result", "final_text": "已写入"}
    assert snapshot[0] == {"role": "system", "content": "写作角色"}
    assert snapshot[2]["reasoning_content"] == "采用动作表达"
    assert snapshot[3]["content"] == "实际写入结果"
    assert snapshot[-1]["content"] == "已写入"
    assert initial == [{"role": "user", "content": "写作需求"}]


@pytest.mark.asyncio
async def test_writer_fork_review_polish_preserves_prefix_and_independent_history(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "语气克制")
    registry = ToolRegistry()
    registry.register(WriteTool())
    model = Model([
        [LLMResponse(type="thinking", content="用动作表达"), LLMResponse(type="tool_use", tool_name="Write", tool_call_id="write",
            tool_input={"path": PATH, "content": "他握着杯子。", "expected_revision_id": "new"}), LLMResponse(type="done")],
        [LLMResponse(type="text_delta", content="已完成写作"), LLMResponse(type="done")],
        [LLMResponse(type="thinking", content="需求已落实，但规则口径仍冲突。"), LLMResponse(type="text_delta", content=REPORT), LLMResponse(type="done")],
        [LLMResponse(type="text_delta", content="无需修改：待确认规则，不贸然改正文。"), LLMResponse(type="done")],
    ])
    runner = SubAgentRunner(model, registry, None, None, working_dir=str(tmp_path))
    presets = {
        "chapter_writer": {"system_prompt": "写作阶段角色", "tools": ["Write"], "inherit_history": False, "max_turns": 3},
        "reviewer": {"system_prompt": "审阅阶段角色，必须分开验收和常规检查", "tools": [], "inherit_history": False, "max_turns": 2},
        "chapter_polisher": {"system_prompt": "润色角色", "tools": [], "inherit_history": False, "max_turns": 2},
    }
    runner.presets_tool.get_preset = presets.get
    session = Session("session", "project")
    evidence = {"history_request_id": "root", "requirement_history_id": root,
                "requirement_event_ids": [f"{root}:input"], "user_input": "语气克制", "target_path": PATH}
    snapshot = []
    chunks = [c async for c in runner.spawn_and_run("chapter_writer", "写第一节，语气克制", session,
        run_id="writer", history_evidence=evidence, context_snapshot=snapshot)]
    writer_unit = store.list_document_units(PATH)[0]
    prefix = deepcopy(snapshot)
    workflow = ReviewPolishWorkflow(runner, str(tmp_path))
    results = [c async for c in workflow.run(session, PATH, "写第一节，语气克制", "operation",
        parent_run_id="writer", history_evidence=evidence, fork_context=snapshot)]
    assert chunks[-1].data["result"] == "已完成写作"
    review_call = model.calls[2]
    assert review_call["messages"][:-1] == prefix
    assert snapshot == prefix
    assert "【服务端阶段切换：审阅】" in review_call["messages"][-1]["content"]
    assert "审阅阶段角色" in review_call["messages"][-1]["content"]
    assert "当前修订：" in review_call["messages"][-1]["content"]
    assert "他握着杯子。" not in review_call["messages"][-1]["content"]
    assert review_call["tools"] is None
    review_id = next(c.data["review_history_id"] for c in results if c.type == "subagent_done" and c.data["workflow"] == "auto_review")
    review = store.get_unit(review_id)
    assert review["kind"] == "review"
    assert review["requirement_history_id"] == root
    assert review["target_history_id"] == writer_unit["history_id"]
    assert review["base_revision_id"] == writer_unit["final_revision_id"]
    assert not review["final_revision_id"]
    assert review["context"][:-1] == prefix
    assert {e["scope"].get("review_section") for e in review["events"]} >= {"requirement", "general"}
    assert review_id not in store.list_analyzable()  # parent is still collecting
    closed = store.close_request("root", "completed", "完成", session_turn_no=1)
    assert review_id in closed and review_id in store.list_analyzable()
    assert store.get_unit(root)["reviews"][0]["history_id"] == review_id
    polisher = store.list_document_units(PATH)[1]
    assert polisher["review_history_id"] == review_id
    assert store.get_unit(review_id)["result_histories"][0]["history_id"] == polisher["history_id"]
    assert all(e["evidence_history_id"] == review_id for e in polisher["events"] if e.get("inherited_review"))
    assert len(store.list_versions(PATH)) == 1  # neither review nor no-change creates a body version
    assert store.list_units(kind="review")["items"][0]["task_summary"]
    assert root_history_ids(store.project_dir, [review_id, writer_unit["history_id"]]) == [review_id, root]
    pending = store.list_analyzable()
    assert pending.index(root) < pending.index(review_id) < pending.index(polisher["history_id"])
    inherited_report = next(e for e in polisher["events"] if e["kind"] == "reviewer_general_report")
    delegation = next(e for e in polisher["events"] if e["kind"] == "main_delegation")
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    analyzer._apply("project", polisher, {"records": [{
        "layer": "insight", "claim": "重复的审阅意见", "source_event_ids": [inherited_report["event_id"], delegation["event_id"]],
    }]})
    assert not FileLifecycleStore(str(tmp_path), "project").list("insight")


@pytest.mark.asyncio
async def test_review_ask_is_preserved_at_requirement_and_precisely_referenced(tmp_path, monkeypatch):
    import asyncio

    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "审阅退出条件")
    session = Session("session", "project")

    async def fake_query(*_args, **kwargs):
        task = asyncio.create_task(kwargs["execute_tool"]("AskUserQuestion", {"questions": [{
            "header": "退出", "question": "保留哪个退出条件？", "options": [{"label": "完成任务", "description": "核心任务完成后才可退出"}],
        }]}, "ask"))
        await asyncio.sleep(0)
        session.question_answers = [["完成任务", "与广播一致"]]
        session.question_event.set()
        assert (await task)[0]
        yield {"type": "result", "final_text": REPORT}

    monkeypatch.setattr("novelagent.core.subagent.query", fake_query)
    runner = SubAgentRunner(None, ToolRegistry(), None, None, working_dir=str(tmp_path))
    runner.presets_tool.get_preset = lambda _: {"system_prompt": "审阅", "tools": ["AskUserQuestion"]}
    chunks = [c async for c in runner.spawn_and_run("reviewer", "审阅退出条件", session, run_id="review", history_evidence={
        "history_request_id": "root", "requirement_history_id": root, "requirement_event_ids": [f"{root}:input"], "target_path": PATH,
    })]
    review = store.get_unit(chunks[-1].data["review_history_id"])
    answer = next(e for e in review["events"] if e["kind"] == "user_answer")
    assert answer["inherited_requirement"] and answer["evidence_history_id"] == root
    assert json.loads(answer["content"])[0]["selected_options"][0]["description"] == "核心任务完成后才可退出"
    assert answer["event_id"] in review["requirement_event_ids"]
    assert not store.list_analyzable()
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM history_events WHERE kind='user_answer'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reviewer_blocks_write_even_when_model_uses_inherited_writer_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteTool())
    model = Model([
        [LLMResponse(type="tool_use", tool_name="Write", tool_call_id="write",
            tool_input={"path": PATH, "content": "不应写入", "expected_revision_id": "new"}), LLMResponse(type="done")],
        [LLMResponse(type="text_delta", content=REPORT), LLMResponse(type="done")],
    ])
    runner = SubAgentRunner(model, registry, None, None, working_dir=str(tmp_path))
    runner.presets_tool.get_preset = lambda _: {"system_prompt": "审阅", "tools": ["Write"], "max_turns": 3}
    chunks = [c async for c in runner.spawn_and_run("reviewer", "审阅", Session("session", "project"),
        fork_context=[{"role": "system", "content": "写作"}, {"role": "assistant", "content": "已写入"}])]
    failed = next(c for c in chunks if c.type == "tool_result")
    assert not failed.data["success"] and "禁止" in failed.data["error"]
    assert not (tmp_path / "project" / PATH).exists()
    assert model.calls[0]["tools"] is None


@pytest.mark.parametrize("layer", ["insight", "memory", "pattern"])
def test_review_sources_are_not_collapsed_into_requirement(tmp_path, layer):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "克制语气")
    store.start_request("review", "session", "", parent_request_id="root")
    store.set_requirement_refs("review", root, [f"{root}:input"])
    review_id = store.start_review("review", PATH)
    store.record_review("review", "检查规则", REPORT)
    store.close_request("review", "completed", REPORT)
    store.close_request("root", "completed", "已审阅")
    files = FileLifecycleStore(str(tmp_path), "project")
    saved = files.write(layer, {"claim": "统一机制口径", "history_ids": [review_id]})
    assert saved["history_ids"] == [review_id]


def test_review_extraction_classifies_general_evidence_and_skips_inherited_only(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    root = store.start_request("root", "session", "克制语气")
    store.start_request("review", "session", "", parent_request_id="root")
    store.set_requirement_refs("review", root, [f"{root}:input"])
    review_id = store.start_review("review", PATH)
    store.record_review("review", "常规检查规则", REPORT)
    store.close_request("review", "completed", REPORT)
    store.close_request("root", "completed", "已审阅")
    unit = store.get_unit(review_id)
    report = next(e for e in unit["events"] if e["kind"] == "reviewer_general_report")
    inherited = next(e for e in unit["events"] if e.get("inherited_requirement"))
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    analyzer._apply("project", unit, {"records": [
        {"layer": "insight", "claim": "统一机制口径", "source_event_ids": [report["event_id"], inherited["event_id"]]},
        {"layer": "memory", "claim": "不能重复提取的用户偏好", "source_event_ids": [inherited["event_id"]]},
    ]})
    files = FileLifecycleStore(str(tmp_path), "project")
    saved = files.list("insight")[0]
    assert saved["history_ids"] == [review_id]
    assert saved["support_sources"] == [f"history:{review_id}"]
    evidence = next(e for e in saved["semantic_evidence"] if e["history_id"] == review_id)
    assert evidence["source_kind"] == "reviewer"
    assert evidence["review_sections"] == ["general"]
    assert next(e for e in saved["semantic_evidence"] if e["history_id"] == root)["source_kind"] == "user"
    assert not files.list("memory")
    assert "reviewer_output" not in {e["kind"] for e in analyzer._evidence_view(unit)["events"]}


def test_context_completion_keeps_newest_body_and_avoids_known_outline():
    fork = [{"role": "user", "content": "已提供卷纲"}, {"role": "assistant", "tool_calls": [{
        "function": {"name": "Edit", "arguments": json.dumps({"new_string": "新片段"})}}]}]
    context = "## 工件\n### 最新正文\n新片段和最终正文\n### 卷纲\n已提供卷纲"
    missing = ReviewPolishWorkflow._missing_context(context, fork)
    assert "新片段和最终正文" in missing
    assert "已提供卷纲" not in missing


def test_failed_review_is_retained_without_creating_file_attempt(tmp_path):
    store = DocumentHistoryStore(tmp_path / "project")
    store.start_request("root", "session", "审阅")
    store.start_request("review", "session", "", parent_request_id="root")
    review = store.start_review("review", PATH)
    store.record_review("review", "尚未核验完规则", "")
    store.close_request("review", "failed", "中断")
    assert store.get_unit(review)["kind"] == "review"
    assert store.get_unit(review)["analysis_status"] == "deferred"
    assert store.list_files()["total"] == 0
    assert not store.list_analyzable()


@pytest.mark.asyncio
async def test_review_history_is_accessible_through_project_api(tmp_path):
    from types import SimpleNamespace
    from novelagent.server.routes.traces import get_project_history, list_project_history

    store = DocumentHistoryStore(tmp_path / "project")
    store.start_request("root", "session", "审阅")
    store.start_request("review", "session", "", parent_request_id="root")
    review_id = store.start_review("review", PATH)
    store.record_review("review", "检查规则", REPORT)
    store.close_request("review", "completed", REPORT)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_loop=SimpleNamespace(working_dir=str(tmp_path)))))
    page = await list_project_history("project", request, kind="review", limit=25, offset=0)
    assert page["total"] == 1
    detail = await get_project_history("project", review_id, request)
    assert detail["kind"] == "review" and detail["assistant_response"] == REPORT
