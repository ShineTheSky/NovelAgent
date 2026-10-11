"""Universal LLM failure capture: mock transport only, no production database."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from novelagent.llm.client import LLMClient, LLMResponse
from novelagent.llm.config_loader import LLMConfig
from novelagent.llm.failures import recorded_bad_case_id, llm_failure_context, is_balance_error
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.storage.models import _generate_title
from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder


class Cases:
    def __init__(self):
        self.records = []

    async def capture(self, **kwargs):
        self.records.append(deepcopy(kwargs))
        return f"case-{len(self.records)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    result = LLMClient(str(tmp_path / "unused.yaml"))
    config = LLMConfig(provider="local_openai", model="test-model", base_url="http://mock/v1",
                       api_key="never-save-this-key", retry={"max_retries": 1})
    monkeypatch.setattr(result.loader, "get_config", lambda *args: config)
    result.bad_case_recorder = Cases()
    return result


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


async def collect(client, **kwargs):
    return [chunk async for chunk in client.chat(
        position=kwargs.pop("position", "main_loop"), messages=[{"role": "user", "content": "需求"}],
        stream=kwargs.pop("stream", False), **kwargs)]


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["main_loop", "sub_agent", "memory_prefetch",
                                     "memory_summary_fallback", "session_title", "case_analysis"])
async def test_every_position_records_error_without_analysis(client, monkeypatch, position):
    transport(monkeypatch, lambda request: httpx.Response(500, json={"error": {"message": "失败"}}))
    chunks = await collect(client, position=position, tag=":test",
        bad_case_context={"project_id": "p", "session_id": "s", "source_trace_id": "tr", "history_id": "h"})
    assert len(chunks) == 1 and chunks[0].type == "error"
    assert recorded_bad_case_id(chunks[0].error) == "case-1"
    record, = client.bad_case_recorder.records
    assert not record["schedule_analysis"]
    assert (record["project_id"], record["session_id"], record["source_trace_id"]) == ("p", "s", "tr")
    assert record["params"]["position"] == position and record["params"]["history_id"] == "h"
    assert record["params"]["attempt"] == 1 and record["duration_ms"] >= 0
    assert record["params"]["http_status"] == 500 and "失败" in record["params"]["response_detail"]
    assert "never-save-this-key" not in json.dumps(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_http_errors_and_provider_short_circuit_are_recorded(client, monkeypatch, status):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "失败"}})
    transport(monkeypatch, respond)
    first = await collect(client)
    second = await collect(client)
    assert first[0].type == second[0].type == "error"
    assert len(client.bad_case_recorder.records) == 2
    assert recorded_bad_case_id(first[0].error) != recorded_bad_case_id(second[0].error)
    assert len(requests) == (2 if status == 400 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["main_loop", "sub_agent", "memory_prefetch", "memory_summary_fallback", "session_title", "case_analysis"])
async def test_balance_errors_propagate_without_badcase_or_repeated_requests(client, monkeypatch, position):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    transport(monkeypatch, respond)
    for _ in range(2):
        chunks = await collect(client, position=position)
        assert chunks[0].type == "error" and is_balance_error(chunks[0].error)
        assert not recorded_bad_case_id(chunks[0].error)
    assert len(requests) == 1 and client.bad_case_recorder.records == []


@pytest.mark.parametrize("error", ["HTTP 402", "HTTP402: failed", "HTTP error 402", "Insufficient Balance",
                                    "insufficient_balance", "Payment Required", "账户余额不足", "余额耗尽", "余额用完"])
def test_balance_error_variants(error):
    assert is_balance_error(RuntimeError(error))


@pytest.mark.parametrize("error", ["HTTP 401", "HTTP 429", "rate limit", "quota exceeded", "timeout", "invalid JSON", "1402 tokens"])
def test_non_billing_errors_are_not_suppressed(error):
    assert not is_balance_error(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_rate_limit_records_each_failed_attempt_not_terminal_wrapper(client, monkeypatch, recover):
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        if recover and count == 2:
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})
        return httpx.Response(429)
    async def no_wait(delay):
        assert delay == 1
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    transport(monkeypatch, respond)
    chunks = await collect(client)
    records = client.bad_case_recorder.records
    assert count == 2 and len(records) == (1 if recover else 2)
    assert [r["params"]["attempt"] for r in records] == ([1] if recover else [1, 2])
    assert len({r["params"]["request_id"] for r in records}) == 1
    assert chunks[-1].type == ("done" if recover else "error")
    if not recover:
        assert recorded_bad_case_id(chunks[-1].error) == "case-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ReadTimeout("慢"), httpx.ConnectError("连接失败")])
async def test_transport_exceptions_are_recorded(client, monkeypatch, exception):
    def respond(request):
        raise exception
    transport(monkeypatch, respond)
    if isinstance(exception, httpx.ReadTimeout):
        chunks = await collect(client)
        assert chunks[0].type == "error" and "超时" in chunks[0].error
    else:
        with pytest.raises(httpx.ConnectError) as raised:
            await collect(client)
        assert recorded_bad_case_id(raised.value) == "case-1"
    assert len(client.bad_case_recorder.records) == 1


@pytest.mark.asyncio
async def test_setup_exception_records_before_rethrow(client, monkeypatch):
    def broken(*args):
        raise ValueError("模型配置不存在")
    monkeypatch.setattr(client.loader, "get_config", broken)
    with pytest.raises(ValueError) as raised:
        await collect(client)
    assert recorded_bad_case_id(raised.value) == "case-1"
    assert client.bad_case_recorder.records[0]["params"]["error_kind"] == "request_setup"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api,anthropic", [("chat_completions", False), ("responses", False), ("chat_completions", True)])
@pytest.mark.parametrize("stream", [False, True])
async def test_protocol_error_payloads_are_not_silently_ignored(client, monkeypatch, wire_api, anthropic, stream):
    config = client.loader.get_config("main_loop")
    config.wire_api = wire_api
    if anthropic:
        config.base_url = "https://api.anthropic.com/v1"
    payload = {"type": "error", "error": {"message": "真实错误"}, "message": "真实错误"}
    if stream:
        response = httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n")
    else:
        response = httpx.Response(200, json=payload)
    transport(monkeypatch, lambda request: response)
    chunks = await collect(client, stream=stream)
    assert len(chunks) == 1 and chunks[0].type == "error" and "真实错误" in chunks[0].error
    assert len(client.bad_case_recorder.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_wire_json_is_recorded(client, monkeypatch, stream):
    transport(monkeypatch, lambda request: httpx.Response(200, text="data: {broken\n\n" if stream else "{broken"))
    if stream:
        chunks = await collect(client, stream=True)
        assert chunks[0].type == "error" and "无效 JSON" in chunks[0].error
    else:
        with pytest.raises(json.JSONDecodeError) as raised:
            await collect(client)
        assert recorded_bad_case_id(raised.value) == "case-1"
    assert len(client.bad_case_recorder.records) == 1


@pytest.mark.asyncio
async def test_invalid_tool_arguments_recorded(client, monkeypatch):
    payload = {"choices": [{"message": {"tool_calls": [{"function": {"name": "Read", "arguments": "{"}}]}}]}
    transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(json.JSONDecodeError):
        await collect(client)
    assert len(client.bad_case_recorder.records) == 1


@pytest.mark.asyncio
async def test_title_fallback_still_records_failure(client, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(500))
    assert await _generate_title(client, [{"role": "user", "content": "需求"}]) == ""
    assert client.bad_case_recorder.records[0]["params"]["position"] == "session_title"


@pytest.mark.asyncio
async def test_knowledge_wrapper_does_not_duplicate_central_error(client, monkeypatch, tmp_path):
    transport(monkeypatch, lambda request: httpx.Response(500))
    pipeline = KnowledgePipeline(client, tmp_path / "workspace", None, "model", bad_cases=client.bad_case_recorder)
    with pytest.raises(RuntimeError) as raised:
        await pipeline._call("p", "提取", "retain", history_id="h")
    await pipeline.capture_failure("p", {"history_id": "h"}, raised.value, "retain")
    assert len(client.bad_case_recorder.records) == 1
    assert client.bad_case_recorder.records[0]["params"]["history_id"] == "h"


@pytest.mark.asyncio
async def test_real_recorder_reuses_error_identity_without_scheduling(client, monkeypatch, tmp_path):
    class Store:
        def __init__(self):
            self.created, self.events = [], []
        async def create_trace(self, *args, **kwargs):
            self.created.append(args)
            return f"bad-{len(self.created)}"
        async def set_trace_operation(self, *args): pass
        async def append_event(self, *args, **kwargs): self.events.append(args)
        async def finish_trace(self, *args): pass
    def forbidden(*args):
        raise AssertionError("Must not schedule model analysis")
    store = Store()
    recorder = AgentBadCaseRecorder(store, str(tmp_path / "workspace"),
                                   SimpleNamespace(classify=lambda _: "llm", schedule=forbidden))
    client.bad_case_recorder = recorder
    transport(monkeypatch, lambda request: httpx.Response(500))
    chunks = await collect(client)
    error = RuntimeError(chunks[0].error)
    assert await recorder.capture(source_trace_id="tr", session_id="s", project_id="p",
        failure_kind="outer_failure", actor="main", error=error) == "bad-1"
    assert len(store.created) == 1
    assert len(list(recorder.directory.glob("*.md"))) == 1
    assert recorded_bad_case_id(deepcopy(chunks[0].error)) == "bad-1"


@pytest.mark.asyncio
async def test_capture_failure_does_not_change_original_error(client, monkeypatch):
    async def broken(**kwargs):
        raise OSError("无法写入")
    monkeypatch.setattr(client.bad_case_recorder, "capture", broken)
    transport(monkeypatch, lambda request: httpx.Response(500))
    chunks = await collect(client)
    assert chunks[0].type == "error" and "500" in chunks[0].error
    assert not recorded_bad_case_id(chunks[0].error)


@pytest.mark.asyncio
async def test_user_cancellation_is_not_model_error(client, monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()
        yield LLMResponse(type="done")
    monkeypatch.setattr(client, "_send_request", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await collect(client)
    assert not client.bad_case_recorder.records


@pytest.mark.asyncio
async def test_scoped_context_is_isolated_between_concurrent_calls(client, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(500))
    async def run(project_id):
        token = llm_failure_context.set({"project_id": project_id, "source_trace_id": "tr-" + project_id})
        try:
            await collect(client, bad_case_context={"history_id": "h-" + project_id})
        finally:
            llm_failure_context.reset(token)
    await asyncio.gather(run("a"), run("b"))
    records = client.bad_case_recorder.records
    assert {(r["project_id"], r["source_trace_id"], r["params"]["history_id"]) for r in records} == {
        ("a", "tr-a", "h-a"), ("b", "tr-b", "h-b")}
    assert llm_failure_context.get() is None


@pytest.mark.asyncio
async def test_valid_tool_argument_deltas_not_recorded_as_errors(client, monkeypatch):
    events = [{"choices": [{"delta": {"tool_calls": [{"id": "read", "function": {"name": "Read", "arguments": '{"path":'}}]}}]},
              {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '"a.md"}'}}]}}]},
              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
    transport(monkeypatch, lambda request: httpx.Response(200, text="".join(
        "data: " + json.dumps(event) + "\n\n" for event in events)))
    chunks = await collect(client, stream=True)
    assert [chunk.type for chunk in chunks] == ["tool_use", "done"]
    assert chunks[0].tool_input == {"path": "a.md"}
    assert not client.bad_case_recorder.records


@pytest.mark.asyncio
async def test_incomplete_final_tool_arguments_recorded(client, monkeypatch):
    events = [{"choices": [{"delta": {"tool_calls": [{"id": "read", "function": {"name": "Read", "arguments": '{"path":'}}]}}]},
              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
    transport(monkeypatch, lambda request: httpx.Response(200, text="".join(
        "data: " + json.dumps(event) + "\n\n" for event in events)))
    chunks = await collect(client, stream=True)
    assert len(chunks) == 1 and chunks[0].type == "error" and "不是完整 JSON" in chunks[0].error
    assert len(client.bad_case_recorder.records) == 1
