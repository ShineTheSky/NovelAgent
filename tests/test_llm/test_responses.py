"""Responses / 本地网关配置回归：所有 HTTP 都使用 MockTransport。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelagent.llm.client import LLMClient
from novelagent.llm.config_loader import LLMConfig
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.models import retrieval_text
from novelagent.server.routes.settings import router
from novelagent.storage import database
from novelagent.trace.store import TraceStore


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "config" / "llm_config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({
        "providers": {"local": {"base_url": "http://localhost:6780/v1", "api_key_env": "TEST_LOCAL_KEY",
                               "wire_api": "responses", "requires_api_key": False,
                               "disable_response_storage": True, "models": ["gpt-5.6"],
                               "headers": {"x-openai-actor-authorization": "local-image-extension"}}},
        "defaults": {"provider": "local", "model": "gpt-5.6"},
        "positions": {"main_loop": {}},
    }), encoding="utf-8")
    return LLMClient(str(path))


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def test_responses_request_replays_tools_without_mutating_messages(client):
    config = client.loader.get_config("main_loop")
    config.reasoning_effort = "high"
    messages = [{"role": "system", "content": "规范"}, {"role": "user", "content": "读正文"},
                {"role": "assistant", "content": "", "reasoning_content": "思考",
                 "tool_calls": [{"id": "call_read", "function": {"name": "Read", "arguments": '{"path":"a.md"}'}}]},
                {"role": "tool_result", "tool_call_id": "call_read", "content": {"text": "正文"}}]
    before = json.dumps(messages)
    tools = [{"name": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    body = client._build_request(config, messages, tools, True)
    assert body["input"][0] == {"role": "developer", "content": "规范"}
    assert body["input"][2]["type"] == "function_call"
    assert body["input"][3] == {"type": "function_call_output", "call_id": "call_read", "output": '{"text": "正文"}'}
    assert body["store"] is False and body["reasoning"] == {"effort": "high"}
    assert "messages" not in body and "temperature" not in body and "max_tokens" not in body
    assert body["tools"][0]["strict"] is False
    assert json.dumps(messages) == before


@pytest.mark.asyncio
async def test_non_stream_responses_headers_json_mode_and_usage(client, monkeypatch):
    monkeypatch.delenv("TEST_LOCAL_KEY", raising=False)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "分析"}]},
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]},
        ], "usage": {"input_tokens": 40, "output_tokens": 5, "total_tokens": 45,
                     "input_tokens_details": {"cached_tokens": 32}}})

    mock_http(monkeypatch, handler)
    chunks = [chunk async for chunk in client.chat("main_loop", [{"role": "user", "content": "JSON"}], stream=False, json_mode=True)]
    request = seen[0]
    assert str(request.url) == "http://localhost:6780/v1/responses"
    assert "authorization" not in request.headers
    assert request.headers["x-openai-actor-authorization"] == "local-image-extension"
    body = json.loads(request.content)
    assert body["store"] is False and body["text"]["format"] == {"type": "json_object"}
    assert [chunk.type for chunk in chunks] == ["thinking", "text_delta", "done"]
    assert chunks[-1].usage["prompt_tokens"] == 40
    assert chunks[-1].usage["prompt_cache_hit_tokens"] == 32


@pytest.mark.asyncio
async def test_stream_emits_tool_once_and_preserves_call_id(client, monkeypatch):
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "思考"},
        {"type": "response.output_text.delta", "delta": "开始"},
        {"type": "response.function_call_arguments.delta", "delta": '{"path":'},
        {"type": "response.function_call_arguments.done", "arguments": '{"path":"a.md"}'},
        {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "real_call_id",
                                                        "name": "Read", "arguments": '{"path":"a.md"}'}},
        {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "function_call"}],
                                                      "usage": {"input_tokens": 10, "output_tokens": 2}}},
    ]
    sse = "".join(f"event: {event['type']}\ndata:{json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    chunks = [chunk async for chunk in client.chat("main_loop", [{"role": "user", "content": "开始"}])]
    assert [chunk.type for chunk in chunks] == ["thinking", "text_delta", "tool_use", "done"]
    assert chunks[2].tool_call_id == "real_call_id" and chunks[2].tool_input == {"path": "a.md"}
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking_event", ["response.reasoning_text.delta", "response.reasoning_summary_text.delta"])
async def test_retain_stream_persists_thinking_json_and_usage(client, monkeypatch, tmp_path, thinking_event):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "traces.sqlite3"))
    await database.init()
    get_config = client.loader.get_config
    monkeypatch.setattr(client.loader, "get_config", lambda *args, **kwargs:
        replace(get_config(*args, **kwargs), reasoning_effort="high"))
    seen = []
    events = [
        {"type": thinking_event, "delta": "先检查"},
        {"type": thinking_event, "delta": "需求来源"},
        {"type": "response.output_text.delta", "delta": '{"memories":'},
        {"type": "response.output_text.delta", "delta": '[]}'},
        {"type": "response.completed", "response": {"status": "completed", "output": [],
            "usage": {"input_tokens": 40, "output_tokens": 8,
                      "output_tokens_details": {"reasoning_tokens": 4}}}},
    ]
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    mock_http(monkeypatch, handler)
    store = TraceStore()
    pipeline = KnowledgePipeline(client, tmp_path / "workspace", None, "test", trace_store=store)
    assert await pipeline._call("p", "提取 Memory JSON", "retain") == {"memories": []}
    assert len(seen) == 1 and seen[0]["stream"] is True
    assert seen[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    traces = await store.list_project_traces("p")
    assert len(traces) == 1 and traces[0]["status"] == "completed"
    saved = await store.list_events(traces[0]["trace_id"])
    assert "".join(e["payload"]["content"] for e in saved if e["event_type"] == "thinking") == "先检查需求来源"
    assert "".join(e["payload"]["content"] for e in saved if e["event_type"] == "text_delta") == '{"memories":[]}'
    usages = [e["payload"] for e in saved if e["event_type"] == "llm_usage"]
    assert len(usages) == 1 and usages[0]["output_tokens_details"]["reasoning_tokens"] == 4
    assert pipeline.store("p").list() == []
    route = next(e["payload"] for e in saved if e["event_type"] == "model_route")
    assert route["reasoning_summary"] == "auto"
    assert pipeline.store("p").model_result(route["request_id"])["response"] == '{"memories":[]}'


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api,base_url", [
    ("responses", "http://localhost:6780/v1"),
    ("chat_completions", "http://localhost:6780/v1"),
    ("responses", "https://api.anthropic.com/v1"),
])
async def test_summary_opt_in_is_responses_only(client, monkeypatch, wire_api, base_url):
    config = replace(client.loader.get_config("main_loop"), wire_api=wire_api,
                     base_url=base_url, reasoning_effort="high")
    monkeypatch.setattr(client.loader, "get_config", lambda *args, **kwargs: config)
    seen = []
    async def send(config, body, stream):
        seen.append(body)
        from novelagent.llm.client import LLMResponse
        yield LLMResponse(type="done")
    monkeypatch.setattr(client, "_send_request", send)
    _ = [chunk async for chunk in client.chat("main_loop", [{"role": "user", "content": "JSON"}],
                                             reasoning_summary="auto")]
    if wire_api == "responses" and not config.is_anthropic:
        assert seen[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    else:
        assert "reasoning" not in seen[0]
        assert "reasoning_summary" not in seen[0]


@pytest.mark.asyncio
async def test_non_retain_analysis_does_not_enable_summary(client, monkeypatch, tmp_path):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"{}"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
            headers={"content-type": "text/event-stream"})
    mock_http(monkeypatch, handler)
    pipeline = KnowledgePipeline(client, tmp_path / "workspace", None, "test")
    assert await pipeline._call("p", "归纳 Insight", "consolidate") == {}
    assert "summary" not in seen[0].get("reasoning", {})


@pytest.mark.asyncio
async def test_retain_summary_never_enters_memory_payload_index_or_identity(client, monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "traces.sqlite3"))
    await database.init()
    package = {"title": "恢复舒缓", "direction": "本段节奏", "requirement": "本段恢复舒缓",
        "background": "前段为快速动作",
        "statements": [{"claim": "本段恢复舒缓", "source_event_ids": ["ev1"],
            "classifications": [{"domain": "writing", "categories": ["polishing"]}]}]}
    expected = {"memories": [package]}
    summary = "ONLY_TRACE_NOT_MEMORY"
    def handler(request):
        assert json.loads(request.content)["reasoning"]["summary"] == "auto"
        events = [{"type": "response.reasoning_summary_text.delta", "delta": summary},
                  {"type": "response.output_text.delta", "delta": json.dumps(expected)},
                  {"type": "response.completed", "response": {"status": "completed"}}]
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events),
                              headers={"content-type": "text/event-stream"})
    mock_http(monkeypatch, handler)
    traces = TraceStore()
    pipeline = KnowledgePipeline(client, tmp_path / "workspace", None, "test", trace_store=traces)
    evidence = [{"event_id": "ev1", "history_id": "h1", "kind": "user_input", "content": "本段恢复舒缓"}]
    records = []
    trace_ids = set()
    for turn in range(2):
        result = await pipeline._call("p", f"提取 Memory {turn}", "retain")
        assert result == expected
        record = pipeline.store("p").save_memory(result["memories"][0], evidence, [1., 0., 0.],
                                                  "test", operation_id="h1:stable")
        assert summary not in json.dumps({"id": record["id"], "payload": record["payload"],
            "evidence": record["evidence"], "index_text": retrieval_text("memory", record["payload"])})
        records.append(record)
        trace = next(t for t in await traces.list_project_traces("p") if t["trace_id"] not in trace_ids)
        saved = await traces.list_events(trace["trace_id"])
        assert "".join(e["payload"]["content"] for e in saved if e["event_type"] == "thinking") == summary
        route = next(e["payload"] for e in saved if e["event_type"] == "model_route")
        assert summary not in pipeline.store("p").model_result(route["request_id"])["response"]
        trace_ids.add(trace["trace_id"])
        summary += "_CHANGED"
    assert records[0]["id"] == records[1]["id"]
    assert records[0]["version"] == records[1]["version"] == 1


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_or_fallback_and_new_key_unblocks(client, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"code": "API_KEY_REQUIRED"})

    mock_http(monkeypatch, handler)
    for _ in range(2):
        chunks = [chunk async for chunk in client.chat("main_loop", [{"role": "user", "content": "hi"}], stream=False)]
        assert chunks[0].type == "error"
    assert len(requests) == 1
    client.set_runtime_provider_settings("local", "http://localhost:6780/v1", "new-test-key", ["gpt-5.6"])
    _ = [chunk async for chunk in client.chat("main_loop", [{"role": "user", "content": "hi"}], stream=False)]
    assert len(requests) == 2 and requests[-1].headers["authorization"] == "Bearer new-test-key"


def test_incomplete_response_is_error_not_success(client):
    chunks = client._parse_non_stream_response(client.loader.get_config("main_loop"), {
        "status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}})
    assert len(chunks) == 1 and chunks[0].type == "error"


def test_existing_chat_completions_route_unchanged(client):
    config = LLMConfig(provider="deepseek", model="test")
    body = client._build_request(config, [{"role": "user", "content": "hi"}], None, False)
    assert body["messages"] == [{"role": "user", "content": "hi"}] and "input" not in body


@pytest.fixture
def settings_api(client):
    app = FastAPI()
    app.include_router(router)
    app.state.llm_config_path = client.loader.config_path
    app.state.agent_loop = SimpleNamespace(llm=client)
    return TestClient(app)


def test_persistent_protocol_headers_roundtrip_and_old_payload_preserves_options(client, settings_api):
    setting = {"base_url": "http://localhost:6780/v1/", "models": ["gpt-5.6", "gpt-5.6-luna"],
               "wire_api": "responses", "requires_api_key": False, "disable_response_storage": True,
               "headers": {"x-openai-actor-authorization": "local-image-extension"}}
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": setting}}).status_code == 200
    # Older frontend only updates URL / models; don't erase protocol or custom headers.
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": {
        "base_url": setting["base_url"], "models": ["gpt-5.6"]}}}).status_code == 200
    config = client.loader.get_config("main_loop")
    assert config.wire_api == "responses" and config.disable_response_storage and not config.requires_api_key
    row = settings_api.get("/api/settings/llm").json()["providers"][0]
    assert row["headers"] == setting["headers"] and row["base_url"] == "http://localhost:6780/v1"
    assert "api_key" not in row


def test_temporary_no_key_settings_do_not_write_to_disk(client, settings_api, monkeypatch):
    monkeypatch.delenv("TEST_LOCAL_KEY", raising=False)
    setting = {"base_url": "http://localhost:6780/v1", "models": ["gpt-5.6"], "storage": "temporary",
               "requires_api_key": False, "wire_api": "responses", "disable_response_storage": True,
               "headers": {"x-openai-actor-authorization": "other-actor"}}
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": setting}}).status_code == 200
    config = client.loader.get_config("main_loop")
    assert config.api_key == "" and config.headers == setting["headers"]
    row = settings_api.get("/api/settings/llm").json()["providers"][0]
    assert row["has_temporary_key"] and not row["is_configured"]
    assert row["headers"] == setting["headers"]
    from pathlib import Path
    assert not Path(client.loader.config_path).with_name("llm_provider_overrides.yaml").exists()


def test_key_required_for_temporary_provider(client, settings_api):
    setting = {"base_url": "http://localhost:6780/v1", "models": ["gpt-5.6"], "storage": "temporary",
               "requires_api_key": True}
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": setting}}).status_code == 400
    assert not client.has_runtime_provider_settings("local")


def test_saved_key_is_not_exposed_to_browser_or_provider_yaml(client, settings_api, monkeypatch):
    monkeypatch.delenv("TEST_LOCAL_KEY", raising=False)
    setting = {"base_url": "http://localhost:6780/v1", "models": ["gpt-5.6"], "api_key": "private-test-key"}
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": setting}}).status_code == 200
    assert client.loader.get_config("main_loop").api_key == "private-test-key"
    assert "private-test-key" not in settings_api.get("/api/settings/llm").text
    from pathlib import Path
    assert "private-test-key" not in Path(client.loader.config_path).with_name("llm_provider_overrides.yaml").read_text()


@pytest.mark.parametrize("headers", [{"X-Test": "value\r\nAuthorization: secret"}, {"Authorization": "Bearer secret"}])
def test_unsafe_headers_rejected_before_settings_change(client, settings_api, headers):
    setting = {"base_url": "http://localhost:6780/v1", "models": ["gpt-5.6"], "headers": headers}
    assert settings_api.put("/api/settings/providers", json={"providers": {"local": setting}}).status_code == 400
    assert client.loader.get_config("main_loop").headers == {"x-openai-actor-authorization": "local-image-extension"}
