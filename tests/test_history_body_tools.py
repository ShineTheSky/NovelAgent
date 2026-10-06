"""Version body reads and retain tool rounds; isolated SQLite, no network."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from novelagent.history import DocumentHistoryStore
from novelagent.llm.client import LLMResponse
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.history_input import HistoryBodyReferences
from novelagent.memory.models import MemoryExtractionPackage
from novelagent.tools.base import ToolContext
from novelagent.tools.search_memory import ReadHistoryTool


def snapshots(tmp_path):
    history = DocumentHistoryStore(tmp_path / "p")
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "v1", None, "旧正文\n第二行\n第三行\n", "旧正文\n第二行\n第三行\n")
    history.record_revision(path, "v2", "v1", "新正文\n", "新正文\n")
    return history


def fragment_input():
    return {"title": "调整正文", "direction": "表达调整", "background": "旧表达", "requirement": "换成新表达",
            "artifact_paths": ["world/a.md"], "facts": [{"domain": "writing", "claim": "用户要求调整表达",
                "source_event_ids": ["user"]}],
            "fragments": [{"source_event_id": "edit", "before": "旧正文", "after": "新正文"}]}


def fragment_evidence(tool="Edit"):
    return [{"event_id": "user", "kind": "user_input", "content": "换成新表达"},
            {"event_id": "edit", "kind": "file_change_attempt", "content": json.dumps({
                "tool": tool, "success": True, "params": {"path": "world/a.md", "expected_revision_id": "v1",
                    "old_string": "旧正文\n第二行\n第三行\n", "new_string": "新正文\n", "content": "新正文\n"},
                "result": "当前修订: v2"})}]


def test_model_fragment_schema_has_no_document_version_fields():
    schema = MemoryExtractionPackage.model_json_schema()["$defs"]["FragmentSelection"]
    assert set(schema["properties"]) == {"source_event_id", "before", "after"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("tool", ["Edit", "Write", "Read"])
def test_fragment_versions_are_filled_from_the_cited_event(tmp_path, tool):
    snapshots(tmp_path)
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    evidence = fragment_evidence(tool)
    if tool == "Read":
        evidence[1].update(kind="document_reference", content=json.dumps({
            "params": {"path": "world/a.md"}, "revision_id": "v2", "content": "新正文\n"}))
    raw = fragment_input()
    original = deepcopy(raw)
    package = pipeline._model_package("p", raw, evidence)
    assert package["fragments"][0] == {"source_event_id": "edit", "path": "world/a.md",
        "revision_id": "v2", "previous_revision_id": "v1", "before": "旧正文", "after": "新正文",
        "verification": "event_verified" if tool == "Edit" else "model_trusted"}
    pipeline._validate_fragments("p", package, {"world/a.md"}, evidence)
    assert package["fragments"][0]["verification"] == "version_verified"
    assert raw == original


@pytest.mark.parametrize("field,value", [("revision_id", "v2"), ("previous_revision_id", "v1"), ("path", "world/a.md")])
def test_model_cannot_fill_fragment_path_or_versions(tmp_path, field, value):
    snapshots(tmp_path)
    raw = fragment_input()
    raw["fragments"][0][field] = value
    with pytest.raises(ValueError, match="Extra inputs"):
        KnowledgePipeline(None, tmp_path, None, "test")._model_package("p", raw, fragment_evidence())


@pytest.mark.parametrize("source", ["missing", "user"])
def test_fragment_source_must_be_a_document_event(tmp_path, source):
    snapshots(tmp_path)
    raw = fragment_input()
    raw["fragments"][0]["source_event_id"] = source
    with pytest.raises(ValueError, match="unavailable document event"):
        KnowledgePipeline(None, tmp_path, None, "test")._model_package("p", raw, fragment_evidence())


def test_failed_edit_cannot_bind_fragment_versions(tmp_path):
    snapshots(tmp_path)
    evidence = fragment_evidence()
    payload = json.loads(evidence[1]["content"])
    payload["success"] = False
    evidence[1]["content"] = json.dumps(payload)
    with pytest.raises(ValueError, match="successful document modification"):
        KnowledgePipeline(None, tmp_path, None, "test")._model_package("p", fragment_input(), evidence)


def test_real_but_unrelated_edit_cannot_supply_fragment_versions(tmp_path):
    history = snapshots(tmp_path)
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "v3", "v2", "新正文\n后来修改\n", "新正文\n后来修改\n")
    evidence = fragment_evidence()
    payload = json.loads(evidence[1]["content"])
    payload.update(params={"path": "world/a.md", "expected_revision_id": "v2",
                           "old_string": "其他句子", "new_string": "后来修改"}, result="当前修订: v3")
    evidence[1]["content"] = json.dumps(payload)
    # The quote exists in v3, but this Edit did not make that change.
    with pytest.raises(ValueError, match="does not match its cited Edit"):
        KnowledgePipeline(None, tmp_path, None, "test")._model_package("p", fragment_input(), evidence)


def test_program_binding_does_not_replace_missing_versions_with_latest(tmp_path):
    snapshots(tmp_path)
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    evidence = fragment_evidence()
    payload = json.loads(evidence[1]["content"])
    payload["result"] = "当前修订: missing"
    evidence[1]["content"] = json.dumps(payload)
    package = pipeline._model_package("p", fragment_input(), evidence)
    assert package["fragments"][0]["revision_id"] == "missing"
    pipeline._validate_fragments("p", package, {"world/a.md"}, evidence)
    assert package["fragments"][0]["verification"] == "event_verified"
    assert package["fragments"][0]["after"] == "新正文"


def test_missing_event_text_and_versions_trust_model_but_do_not_invent_versions(tmp_path):
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    evidence = fragment_evidence()
    payload = json.loads(evidence[1]["content"])
    del payload["params"]["old_string"]
    del payload["params"]["new_string"]
    evidence[1]["content"] = json.dumps(payload)
    package = pipeline._checked_model_package("p", fragment_input(), evidence, {"world/a.md"}, "memories[0]")
    fragment = package["fragments"][0]
    assert fragment["verification"] == "model_trusted"
    assert (fragment["before"], fragment["after"]) == ("旧正文", "新正文")
    assert (fragment["revision_id"], fragment["previous_revision_id"]) == ("v2", "v1")
    assert DocumentHistoryStore(tmp_path / "p").get_revision("v2") is None


def test_missing_previous_version_keeps_before_fragment(tmp_path):
    history = DocumentHistoryStore(tmp_path / "p")
    history.record_revision(tmp_path / "p" / "world/a.md", "v2", None, "新正文", "新正文")
    evidence = fragment_evidence()
    package = KnowledgePipeline(None, tmp_path, None, "test")._checked_model_package(
        "p", fragment_input(), evidence, {"world/a.md"}, "memories[0]")
    assert package["fragments"][0]["before"] == "旧正文"
    assert package["fragments"][0]["verification"] == "event_verified"


def test_available_empty_body_is_not_treated_as_missing(tmp_path):
    history = DocumentHistoryStore(tmp_path / "p")
    history.record_revision(tmp_path / "p" / "world/a.md", "v2", None, "", "")
    evidence = fragment_evidence()
    with pytest.raises(ValueError, match="cannot be verified"):
        KnowledgePipeline(None, tmp_path, None, "test")._checked_model_package(
            "p", fragment_input(), evidence, {"world/a.md"}, "memories[0]")


def test_available_write_content_still_rejects_wrong_fragment_without_archive(tmp_path):
    evidence = fragment_evidence("Write")
    raw = fragment_input()
    raw["fragments"][0]["after"] = "另一段正文"
    with pytest.raises(ValueError, match="does not match its cited Write"):
        KnowledgePipeline(None, tmp_path, None, "test")._checked_model_package(
            "p", raw, evidence, {"world/a.md"}, "memories[0]")


def test_stored_fragment_cannot_override_its_source_versions(tmp_path):
    snapshots(tmp_path)
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    evidence = fragment_evidence()
    package = pipeline._model_package("p", fragment_input(), evidence)
    package["fragments"][0]["previous_revision_id"] = "fabricated"
    with pytest.raises(ValueError, match="versions do not match"):
        pipeline._validate_fragments("p", package, {"world/a.md"}, evidence)


@pytest.mark.asyncio
async def test_version_read_is_paginated_body_only_and_never_latest(tmp_path):
    history = snapshots(tmp_path)
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    tool, context = ReadHistoryTool(pipeline), ToolContext("s", "p", str(tmp_path / "p"))
    result = await tool.execute({"revision_id": "v1", "limit": 2}, context)
    data = json.loads(result.data)
    assert result.success and data["body"] == "旧正文\n第二行\n"
    assert data["revision_id"] == "v1" and data["total_lines"] == 3 and data["next_offset"] == 3
    assert "evidence" not in data and "rendered_md" not in data
    page = json.loads((await tool.execute({"revision_id": "v1", "offset": 3}, context)).data)
    assert page["body"] == "第三行\n" and page["next_offset"] is None
    assert json.loads((await tool.execute({"revision_id": "v2"}, context)).data)["body"] == "新正文\n"
    assert history.latest("world/a.md")["revision_id"] == "v2"


@pytest.mark.asyncio
async def test_missing_version_is_explicit_and_does_not_repair_from_file(tmp_path):
    history = snapshots(tmp_path)
    path = tmp_path / "p" / "world" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("不应冒充历史的当前文件", encoding="utf-8")
    pipeline = KnowledgePipeline(None, tmp_path, None, "test")
    result = await ReadHistoryTool(pipeline).execute({"revision_id": "absent"}, ToolContext("s", "p", str(tmp_path / "p")))
    assert not result.success and result.error == "正文版本缺失"
    assert json.loads(result.data) == {"status": "missing", "revision_id": "absent"}
    assert history.get_revision("absent") is None and path.read_text(encoding="utf-8") == "不应冒充历史的当前文件"


@pytest.mark.asyncio
async def test_empty_body_and_out_of_range_are_not_missing(tmp_path):
    history = snapshots(tmp_path)
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "v3", "v2", "", "")
    tool = ReadHistoryTool(KnowledgePipeline(None, tmp_path, None, "test"))
    context = ToolContext("s", "p", str(tmp_path / "p"))
    for params in ({"revision_id": "v3"}, {"revision_id": "v1", "offset": 100}):
        result = await tool.execute(params, context)
        assert result.success and json.loads(result.data)["body"] == ""
        assert json.loads(result.data)["status"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"revision_id": "v1", "offset": 0}, {"revision_id": "v1", "limit": False},
    {"revision_id": "v1", "limit": 2001}, {"revision_id": "v1", "history_id": "h"},
    {"revision_id": []}, {"revision_id": ""},
])
async def test_invalid_body_read_params(tmp_path, params):
    snapshots(tmp_path)
    result = await ReadHistoryTool(KnowledgePipeline(None, tmp_path, None, "test")).execute(
        params, ToolContext("s", "p", str(tmp_path / "p")))
    assert not result.success


class ReadingModel:
    def __init__(self, requests):
        self.requests, self.calls = requests, []

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if len(self.calls) == 1:
            yield LLMResponse(type="thinking", content="先核验版本")
            yield LLMResponse(type="text_delta", content="读取后再提取，不是最终JSON")
            for params in self.requests:
                yield LLMResponse(type="tool_use", tool_name=params.pop("tool", "ReadHistory"), tool_input=params)
        else:
            yield LLMResponse(type="text_delta", content='{"memories":[]}')
        yield LLMResponse(type="done", usage={"total_tokens": 10})


@pytest.mark.asyncio
async def test_retain_reads_multiple_versions_missing_and_caches_final_only(tmp_path):
    snapshots(tmp_path)
    model = ReadingModel([{"revision_id": "v1"}, {"revision_id": "v2"}, {"revision_id": "missing"}])
    pipeline = KnowledgePipeline(model, tmp_path, None, "test")
    assert await pipeline._call("p", "提取 Memory JSON", "retain") == {"memories": []}
    assert len(model.calls) == 2
    sent = model.calls[1]["messages"]
    assistant = next(m for m in sent if m.get("tool_calls"))
    replies = [m for m in sent if m["role"] == "tool_result"]
    assert [c["id"] for c in assistant["tool_calls"]] == [m["tool_call_id"] for m in replies]
    assert len({m["tool_call_id"] for m in replies}) == 3
    assert assistant["reasoning_content"] == "先核验版本"
    results = [json.loads(m["content"]) for m in replies]
    assert json.loads(results[0]["data"])["body"].startswith("旧正文")
    assert json.loads(results[1]["data"])["body"].startswith("新正文")
    assert not results[2]["success"] and json.loads(results[2]["data"])["status"] == "missing"
    assert model.calls[0]["tools"][0]["parameters"]["required"] == ["revision_id"]
    assert await pipeline._call("p", "提取 Memory JSON", "retain") == {"memories": []}
    assert len(model.calls) == 2
    with pipeline.store("p").connect() as db:
        cached = db.execute("SELECT response,usage FROM model_results").fetchone()
    assert cached["response"] == '{"memories":[]}' and json.loads(cached["usage"])["total_tokens"] == 20


@pytest.mark.asyncio
async def test_tool_error_is_recorded_even_when_extraction_succeeds(tmp_path):
    snapshots(tmp_path)
    model = ReadingModel([{"revision_id": "missing"}, {"tool": "Write", "path": "world/b.md"}])
    class Recorder:
        def __init__(self): self.calls = []
        async def capture(self, **kwargs):
            self.calls.append(kwargs)
            return "bad-case"
    recorder = Recorder()
    pipeline = KnowledgePipeline(model, tmp_path, None, "test", bad_cases=recorder)
    assert await pipeline._call("p", "提取", "retain", history_id="h") == {"memories": []}
    assert len(recorder.calls) == 2
    assert all(c["params"]["error_kind"] == "tool" and c["schedule_analysis"] is False for c in recorder.calls)
    assert recorder.calls[0]["params"]["tool_params"] == {"revision_id": "missing"}
    assert recorder.calls[1]["params"]["tool"] == "Write"


@pytest.mark.asyncio
async def test_retain_cannot_execute_write_or_load_entire_history(tmp_path):
    snapshots(tmp_path)
    model = ReadingModel([{"tool": "Write", "path": "world/b.md", "content": "禁止写入"},
                          {"history_id": "h"}])
    pipeline = KnowledgePipeline(model, tmp_path, None, "test")
    await pipeline._call("p", "提取 Memory JSON", "retain")
    replies = [m for m in model.calls[1]["messages"] if m["role"] == "tool_result"]
    assert all(not json.loads(m["content"])["success"] for m in replies)
    assert not (tmp_path / "p" / "world" / "b.md").exists()


@pytest.mark.asyncio
async def test_tool_body_is_budget_checked_before_next_model_call(tmp_path):
    history = snapshots(tmp_path)
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "large", "v2", "超长正文" * 10000, "超长正文" * 10000)
    model = ReadingModel([{"revision_id": "large"}])
    pipeline = KnowledgePipeline(model, tmp_path, None, "test", config={"analysis_input_tokens": 4000})
    with pytest.raises(ValueError, match="input budget"):
        await pipeline._call("p", "提取 Memory JSON", "retain")
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_read_rounds_are_bounded_by_configured_max_turns(tmp_path):
    snapshots(tmp_path)
    class Model:
        def __init__(self): self.calls = []
        async def chat(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            yield LLMResponse(type="tool_use", tool_name="ReadHistory", tool_input={"revision_id": "v1"})
    model = Model()
    pipeline = KnowledgePipeline(model, tmp_path, None, "test", config={"analysis_max_turns": 3})
    with pytest.raises(RuntimeError, match="max_turns=3"):
        await pipeline._call("p", "提取 Memory JSON", "retain")
    assert len(model.calls) == 3
    assert all(c["tools"] == model.calls[0]["tools"] for c in model.calls)
    with pipeline.store("p").connect() as db:
        assert db.execute("SELECT count(*) FROM model_results").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_turns,success", [(2, False), (3, True)])
async def test_tool_and_validation_rounds_share_the_same_limit(tmp_path, max_turns, success):
    snapshots(tmp_path)
    class Model:
        def __init__(self): self.calls = []
        async def chat(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            if len(self.calls) == 1:
                yield LLMResponse(type="tool_use", tool_name="ReadHistory", tool_input={"revision_id": "v1"})
            else:
                yield LLMResponse(type="text_delta", content=json.dumps({"ok": len(self.calls) >= 3}))
    model = Model()
    pipeline = KnowledgePipeline(model, tmp_path, None, "test", config={"analysis_max_turns": max_turns})
    def validate(result):
        if not result["ok"]:
            raise ValueError("ok 字段应为 true，实际为 false")
    if success:
        assert await pipeline._call("p", "提取", "retain", validate=validate) == {"ok": True}
    else:
        with pytest.raises(RuntimeError, match="max_turns=2"):
            await pipeline._call("p", "提取", "retain", validate=validate)
    assert len(model.calls) == max_turns
    assert all(c["tools"] == model.calls[0]["tools"] for c in model.calls)


@pytest.mark.asyncio
async def test_anthropic_read_messages_use_content_blocks(tmp_path):
    snapshots(tmp_path)
    model = ReadingModel([{"revision_id": "v1"}])
    model.loader = SimpleNamespace(get_config=lambda *a: SimpleNamespace(provider="anthropic", model="test",
        base_url="unused", temperature=0, reasoning_effort=None, is_anthropic=True))
    await KnowledgePipeline(model, tmp_path, None, "test")._call("p", "提取 Memory JSON", "retain")
    messages = model.calls[1]["messages"]
    assistant, reply = messages[-2:]
    assert assistant["role"] == "assistant" and reply["role"] == "user"
    call = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert call["id"] == reply["content"][0]["tool_use_id"]
    assert reply["content"][0]["type"] == "tool_result"


def test_body_references_do_not_mutate_evidence_and_mark_missing_reads(tmp_path):
    history = snapshots(tmp_path)
    bodies = HistoryBodyReferences(history)
    evidence = [{"kind": "document_reference", "content": json.dumps({
        "path": "world/a.md", "revision_id": "v1", "params": {"offset": 2}, "content": "第二行"})},
        {"kind": "file_change_attempt", "content": json.dumps({"tool": "Write", "success": True,
            "params": {"path": "world/a.md", "content": "新正文\n"}, "result": "当前修订: v2"})},
        {"kind": "document_reference", "content": json.dumps({"params": {"path": "world/a.md"},
            "revision_id": "missing", "content": "原始片段仍应保存"})}]
    original = deepcopy(evidence)
    compact = [json.loads(e["content"]) for e in bodies.events(evidence)]
    assert "content" not in compact[0] and compact[0]["params"]["offset"] == 2
    assert "content" not in compact[1]["params"] and compact[1]["body_reference"]["revision_id"] == "v2"
    assert compact[2]["body_reference"]["status"] == "missing" and "content" not in compact[2]
    assert evidence == original


def test_failed_or_mismatched_writes_are_not_presented_as_versions(tmp_path):
    bodies = HistoryBodyReferences(snapshots(tmp_path))
    for success, content in ((False, "新正文\n"), (True, "不是这个版本的正文")):
        payload = {"tool": "Write", "success": success, "params": {"path": "world/a.md", "content": content},
                   "result": "当前修订: v2"}
        assert bodies.write(deepcopy(payload)) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_context", [False, True])
async def test_retain_prompt_uses_version_directory_instead_of_bodies(tmp_path, inherited_context):
    history = snapshots(tmp_path)
    source = {"history_id": "h", "kind": "conversation", "status": "completed", "events": [
        {"event_id": "read", "history_id": "h", "kind": "document_reference", "content": json.dumps({
            "params": {"path": "world/a.md"}, "revision_id": "v1", "content": "旧正文\n第二行\n第三行\n"})}],
        "revisions": [history.get_revision("v2")]}
    if inherited_context:
        source["review_workflow"] = {"context": [
            {"role": "assistant", "reasoning_content": "核验规则的思考", "tool_calls": [
                {"id": "read1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"world/a.md"}'}},
                {"id": "write1", "type": "function", "function": {"name": "Write", "arguments": json.dumps({"path": "world/a.md", "content": "新正文\n"})}}]},
            {"role": "tool_result", "tool_call_id": "read1", "content": "[当前修订: v1]\n旧正文\n第二行\n第三行\n"},
            {"role": "tool_result", "tool_call_id": "write1", "content": "文件已写入\n当前修订: v2"}]}
    original = deepcopy(source)
    class Model:
        async def chat(self, **kwargs):
            self.messages = deepcopy(kwargs["messages"])
            yield LLMResponse(type="text_delta", content='{"memories":[]}')
    model = Model()
    pipeline = KnowledgePipeline(model, tmp_path, None, "test")
    await pipeline.process("p", source)
    text = json.dumps(model.messages, ensure_ascii=False)
    assert "旧正文" not in text and "新正文" not in text and "第二行" not in text
    prompt = model.messages[-1]["content"]
    data = json.loads(prompt.split("\nHistory:", 1)[1])
    assert {r["revision_id"] for r in data["body_versions"]} == {"v1", "v2"}
    assert all("body" not in a and "diff" not in a for a in data["artifacts"])
    assert source == original and history.get_revision("v1")["body_md"].startswith("旧正文")
    if inherited_context:
        assert "核验规则的思考" in text


def test_history_api_reference_view_hides_bodies_without_changing_stored_history(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from novelagent.server.routes.traces import router
    history = snapshots(tmp_path)
    history.start_request("request", "s", "检查世界观")
    history.append_request_event("request", "document_reference", json.dumps({
        "path": "world/a.md", "revision_id": "v1", "content": "旧正文\n第二行\n第三行\n"}))
    history.append_request_event("request", "document_reference", json.dumps({
        "path": "world/missing.md", "revision_id": "absent", "content": "缺失版本的旧快照"}))
    history.append_document_event("request", "world/a.md", "file_change_attempt", json.dumps({
        "tool": "Write", "params": {"path": "world/a.md", "content": "新正文\n"},
        "success": True, "result": "当前修订: v2"}))
    hid = history._unit_id("request", "request")
    file_hid = history._unit_id("request", "document", "world/a.md")
    history.close_request("request", "completed", "检查完成", session_turn_no=1)
    app = FastAPI()
    app.state.agent_loop = SimpleNamespace(working_dir=str(tmp_path))
    app.include_router(router)
    original = history.get_unit(hid)
    with TestClient(app) as client:
        data = client.get(f"/api/projects/p/history/{hid}?body_references=true").json()
        assert "旧正文" not in json.dumps(data["events"], ensure_ascii=False)
        assert "缺失版本的旧快照" not in json.dumps(data["events"], ensure_ascii=False)
        versions = {v["revision_id"]: v["status"] for v in data["body_versions"]}
        assert versions == {"v1": "available", "absent": "missing"}
        file_data = client.get(f"/api/projects/p/history/{file_hid}?body_references=true").json()
        assert "新正文" not in json.dumps(file_data["events"], ensure_ascii=False)
        assert client.get(f"/api/projects/p/history/{hid}").json()["events"] == original["events"]
    assert history.get_unit(hid)["events"] == original["events"]
