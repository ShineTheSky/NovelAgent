"""Offline regressions for observed model/publication failures. No production DB."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.history.store import DocumentHistoryStore
from novelagent.llm.client import LLMClient, LLMResponse, ProviderBlockedError
from novelagent.llm.config_loader import LLMConfig
from novelagent.llm.json_output import json_object
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.models import Insight, MemoryPackage, TimeAnchor
from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer
from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.tools.base import PermissionResult, ToolResult
from novelagent.core.llm_turn import query


def package():
    return MemoryPackage.model_validate({
        "title": "自然承接", "direction": "段落承接", "background": "连续短句", "requirement": "自然一些",
        "facts": [{"domain": "writing", "claim": "用动作自然承接", "source_event_ids": ["ev"]},
                  {"domain": "writing", "claim": "保留信息", "source_event_ids": ["ev"]}],
    }).model_dump()


def evidence():
    return [{"event_id": "ev", "history_id": "h", "kind": "user_input", "content": "自然一些"}]


class Model:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        yield LLMResponse(type="text_delta", content=self.text)
        yield LLMResponse(type="done", usage={"total_tokens": 8, "prompt_cache_hit_tokens": 3})


def test_same_source_different_facts_publish_without_collision(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memory(package(), evidence(), [1, 0], "model")
    a, b = [f["fact_id"] for f in memory["payload"]["facts"]]
    source = {"memory_id": memory["id"], "version": 1, "role": "support"}
    payload = {"title": "自然承接", "claim": "保留信息并自然承接", "kind": "method", "purpose": "流畅",
               "sources": [{**source, "fact_ids": [a]}, {**source, "fact_ids": [b]},
                           {**source, "role": "context", "fact_ids": [a]}]}
    result = store.publish_insights([{"payload": payload, "evidence": evidence(), "vector": [1, 0],
                                     "model_id": "model"}], {memory["id"]: 1})[0]
    assert result["payload"]["sources"][0]["fact_ids"] == [a, b]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM derivations").fetchone()[0] == 2


def test_all_facts_source_keeps_all_facts_semantics():
    payload = {"title": "方法", "claim": "承接", "kind": "method", "purpose": "流畅",
               "sources": [{"memory_id": "m", "version": 1, "fact_ids": []},
                           {"memory_id": "m", "version": 1, "fact_ids": ["f"]}]}
    assert Insight.model_validate(payload).sources[0].fact_ids == []


def test_empty_fact_ids_resolve_positional_internal_relations(tmp_path):
    raw = package()
    raw["relations"] = [{"kind": "causal", "predicate": "supports", "source_fact_id": "new:1",
                         "target_fact_id": "new:2", "target_version": 1, "source_event_ids": ["ev"]}]
    memory = KnowledgeStore(tmp_path).save_memory(raw, evidence(), [1, 0], "model")
    relation = memory["payload"]["relations"][0]
    assert relation["source_fact_id"] == memory["payload"]["facts"][0]["fact_id"]
    assert relation["target_fact_id"] == memory["payload"]["facts"][1]["fact_id"]


@pytest.mark.asyncio
async def test_completed_response_survives_pipeline_restart_without_model_call(tmp_path):
    model = Model('{"insights": [], "withdrawals": []}')
    pipeline = KnowledgePipeline(model, tmp_path, None, "model")
    first = await pipeline._call("p", "只输出 JSON", "consolidate")
    restarted = KnowledgePipeline(model, tmp_path, None, "model")
    assert await restarted._call("p", "只输出 JSON", "consolidate") == first
    assert len(model.calls) == 1 and model.calls[0]["json_mode"]


@pytest.mark.asyncio
async def test_invalid_json_gets_feedback_until_max_turns_without_success_cache(tmp_path):
    model = Model('{"memories": [}')
    pipeline = KnowledgePipeline(model, tmp_path, None, "model", config={"analysis_max_turns": 3})
    with pytest.raises(RuntimeError, match="max_turns=3"):
        await pipeline._call("p", "只输出 JSON", "retain")
    assert len(model.calls) == 3
    assert "校验详情" in model.calls[-1]["messages"][-1]["content"]
    with pipeline.store("p").connect() as db:
        assert db.execute("SELECT count(*) FROM model_results").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_schema_invalid_output_is_only_prepared_after_successful_repair(tmp_path):
    raw = package()
    raw["facts"][0]["domain"] = "outline"
    model = Model(json.dumps({"memories": [raw]}))
    pipeline = KnowledgePipeline(model, tmp_path, None, "model", config={"analysis_max_turns": 2})
    unit = {"history_id": "h", "kind": "conversation", "status": "completed", "events": evidence()}
    for _ in range(2):
        with pytest.raises(RuntimeError, match="max_turns=2"):
            await pipeline.process("p", unit)
    assert len(model.calls) == 4
    with pipeline.store("p").connect() as db:
        assert json.loads(db.execute("SELECT prepared FROM inputs").fetchone()[0]) == {}
        assert db.execute("SELECT count(*) FROM model_results").fetchone()[0] == 0


def test_allowed_artifacts_include_file_history_even_without_saved_revision():
    unit = {"affected_files": [{"path": "chapters/content_1.1.1.md"}]}
    assert "chapters/content_1.1.1.md" in KnowledgePipeline._allowed_paths(unit, [], [])
    events = [{"content": "请修改 outline.md", "scope": {}}]
    assert KnowledgePipeline._allowed_paths({}, events, []) == {"outline.md"}
    assert "world/invented.md" not in KnowledgePipeline._allowed_paths({}, events, [])


def test_source_quote_rejects_paraphrase_and_accepts_exact_excerpt():
    raw = package()
    raw["source_text"] = "自然一些"
    KnowledgePipeline._validate_package(raw, evidence())
    raw["source_text"] = "用户：自然一些"
    with pytest.raises(ValueError, match="exact excerpt"):
        KnowledgePipeline._validate_package(raw, evidence())


def test_structured_ask_answer_verbatim_quote_is_accepted():
    raw = package()
    raw["source_text"] = "保留信息\n自然承接"
    event = {**evidence()[0], "kind": "user_answer",
             "content": json.dumps([{"answer": "保留信息\n自然承接"}], ensure_ascii=False)}
    KnowledgePipeline._validate_package(raw, [event])


@pytest.mark.parametrize("source,quote", [
    ('系统不再报\\"随身物：某某，属性：无\\"。', '系统不再报“随身物：某某，属性：无”。'),
    ('系统不再报\\"随身物：某某，属性：无\\"。', '系统不再报"随身物：某某，属性：无"。'),
    ('系统不再报\\\\"随身物：某某，属性：无\\\\"。', '系统不再报\\"随身物：某某，属性：无\\"。'),
    ("不写‘未知’。", "不写'未知'。"),
])
@pytest.mark.parametrize("kind", ["user_input", "user_answer"])
def test_source_quote_accepts_only_quote_format_differences(source, quote, kind):
    raw = package()
    raw["source_text"] = quote
    content = source if kind == "user_input" else json.dumps([{"answer": source}], ensure_ascii=False)
    event = {**evidence()[0], "kind": kind, "content": content}
    result = KnowledgePipeline._validate_package(raw, [event])
    assert result["source_text"] == quote


@pytest.mark.parametrize("quote", [
    '系统确认“随身物：某某，属性：无”。',
    '系统不再报“随身物:某某，属性：无”。',
    '用户：系统不再报“随身物：某某，属性：无”。',
    '系统不再报 “随身物：某某，属性：无”。',
    '系统不再报“随身物：某某，属性：无”。进去后确认。',
])
def test_source_quote_tolerance_still_rejects_rewriting(quote):
    raw = package()
    raw["source_text"] = quote
    event = {**evidence()[0], "content": '系统不再报\\"随身物：某某，属性：无\\"。'}
    with pytest.raises(ValueError, match="exact excerpt"):
        KnowledgePipeline._validate_package(raw, [event])


def test_source_quote_tolerance_does_not_join_separate_answers():
    raw = package()
    raw["source_text"] = '保留“信息”自然承接'
    event = {**evidence()[0], "kind": "user_answer",
             "content": json.dumps([{"answer": '保留"信息"'}, {"answer": "自然承接"}], ensure_ascii=False)}
    with pytest.raises(ValueError, match="exact excerpt"):
        KnowledgePipeline._validate_package(raw, [event])


def test_incomplete_chapter_anchor_is_preserved_not_invented():
    anchor = TimeAnchor(dimension="chapter", start="1.2").model_dump()
    assert anchor["raw"] == "1.2" and anchor["start"] == anchor["end"] == ""
    assert TimeAnchor(dimension="chapter", start="1.2.3").start == "1.2.3"


def test_nonstream_preserves_usage_reasoning_finish_and_tool_id(tmp_path):
    client = LLMClient(str(tmp_path / "llm.yaml"))
    config = LLMConfig(provider="deepseek", model="test", base_url="https://api.deepseek.com")
    response = {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "分析",
                  "content": "{}", "tool_calls": [{"id": "actual-id", "function": {"name": "Read", "arguments": "{}"}}]}}],
                "usage": {"total_tokens": 42, "prompt_cache_hit_tokens": 12, "prompt_cache_miss_tokens": 20}}
    chunks = client._parse_non_stream_response(config, response)
    assert next(c for c in chunks if c.type == "thinking").content == "分析"
    assert next(c for c in chunks if c.type == "tool_use").tool_call_id == "actual-id"
    assert chunks[-1].usage == response["usage"] and chunks[-1].finish_reason == "length"


@pytest.mark.asyncio
async def test_balance_error_blocks_next_position_on_same_provider(tmp_path, monkeypatch):
    client = LLMClient(str(tmp_path / "llm.yaml"))
    config = LLMConfig(provider="deepseek", model="test", base_url="https://api.deepseek.com")
    monkeypatch.setattr(client.loader, "get_config", lambda *args: config)
    requests = []

    async def failed(*args):
        requests.append(args)
        raise ProviderBlockedError("HTTP 402: Insufficient Balance")
        yield

    monkeypatch.setattr(client, "_send_request", failed)
    for position in ("memory_summary_fallback", "case_analysis", "main_loop"):
        chunks = [c async for c in client.chat(position, [{"role": "user", "content": "JSON"}])]
        assert chunks[0].type == "error"
    assert len(requests) == 1


def test_http402_is_not_a_retryable_generic_api_error(tmp_path):
    client = LLMClient(str(tmp_path / "llm.yaml"))
    with pytest.raises(ProviderBlockedError):
        client._check_status(402, "unused", {}, {}, '{"error":{"message":"Insufficient Balance"}}')


@pytest.mark.asyncio
async def test_recovery_does_not_start_unrequested_legacy_extraction(tmp_path):
    history = DocumentHistoryStore(tmp_path / "p")
    history.start_request("r", "s", "自然一些")
    history.close_request("r", "completed", "完成")
    model = Model('{}')
    analyzer = HistoryAnalyzer(model, str(tmp_path), None,
                               knowledge_pipeline=KnowledgePipeline(model, tmp_path, None, "model"))
    await analyzer.recover()
    assert not model.calls


def test_local_startup_recovery_marks_interrupted_without_models(tmp_path):
    history = DocumentHistoryStore(tmp_path / "p")
    hid = history.start_request("r", "s", "未完成需求")
    analyzer = HistoryAnalyzer(None, str(tmp_path), None)
    assert analyzer.interrupt_collecting() >= 1
    assert history.get_unit(hid)["status"] == "interrupted"
    assert history.get_unit(hid)["events"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "minimax"])
async def test_json_mode_is_only_sent_to_verified_provider(tmp_path, monkeypatch, provider):
    client = LLMClient(str(tmp_path / "llm.yaml"))
    config = LLMConfig(provider=provider, model="test")
    monkeypatch.setattr(client.loader, "get_config", lambda *args: config)
    requests = []

    async def send(config, body, stream):
        requests.append(body)
        yield LLMResponse(type="text_delta", content="{}")
        yield LLMResponse(type="done")

    monkeypatch.setattr(client, "_send_request", send)
    _ = [c async for c in client.chat("case_analysis", [{"role": "user", "content": "JSON"}], json_mode=True)]
    assert ("response_format" in requests[0]) == (provider == "deepseek")


@pytest.mark.asyncio
async def test_badcase_context_never_nests_original_requests(tmp_path):
    class Trace:
        async def list_events(self, *args, **kwargs):
            return [{"event_type": "agent_request_message", "payload": {"content": "secret-body" * 2000}} for _ in range(100)] + [
                {"event_type": "error", "payload": {"message": "publication failed"}}]

    analyzer = BadCaseAnalyzer(None, str(tmp_path / "workspace"), trace_store=Trace())
    context = await analyzer._load_trace_context("trace")
    serialized = json.dumps(context)
    assert "secret-body" not in serialized and len(serialized) < 7000
    assert any(e.get("message") == "publication failed" for e in context)


@pytest.mark.asyncio
async def test_identical_badcase_batch_only_analyzed_once(tmp_path):
    model = Model('{"summary":"原因","root_causes":[],"recommendations":[],"needs_more_evidence":true}')
    analyzer = BadCaseAnalyzer(model, str(tmp_path / "workspace"))
    cases = [{"id": "case", "body": "## 错误\npublication failed"}]
    await analyzer._analyze("publication", cases)
    await analyzer._analyze("publication", deepcopy(cases))
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_failed_publication_retries_cached_response_not_model(tmp_path, monkeypatch):
    class Embedding:
        async def encode(self, texts):
            return [[1, 0] for _ in texts]

    class Retrieval:
        async def recall(self, *args, **kwargs):
            return {"status": "empty", "items": []}

    model = Model('{}')
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "model")
    store = pipeline.store("p")
    memory = store.save_memory(package(), evidence(), [1, 0], "model")
    model.text = json.dumps({"insights": [{"payload": {"title": "承接", "claim": "自然衔接", "kind": "method",
        "purpose": "流畅", "sources": [{"memory_id": memory["id"], "version": 1}]}}], "withdrawals": []})
    monkeypatch.setattr(pipeline, "store", lambda *args: store)
    monkeypatch.setattr(pipeline, "retrieval", lambda *args: Retrieval())
    original = store.publish_insights
    failures = []

    def publish(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("simulated publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "publish_insights", publish)
    await pipeline.consolidate_pending("p")
    assert store.jobs()[0]["status"] == "failed"
    await pipeline.consolidate_pending("p")
    assert store.jobs() and len(model.calls) == 1  # Ordinary new work does not retry a failed job.
    await pipeline.consolidate_pending("p", retry_failed=True)
    assert not store.jobs() and len(model.calls) == 1


@pytest.mark.asyncio
async def test_missing_support_facts_do_not_publish_or_repeat_model(tmp_path, monkeypatch):
    class Retrieval:
        async def recall(self, *args, **kwargs):
            return {"status": "empty", "items": []}

    model = Model('{}')
    pipeline = KnowledgePipeline(model, tmp_path, None, "model")
    memory = pipeline.store("p").save_memory(package(), evidence(), [1, 0], "model")
    model.text = json.dumps({"insights": [{"payload": {"title": "不确定", "claim": "推测", "kind": "fact",
        "purpose": "参考", "sources": [{"memory_id": memory["id"], "version": 1, "role": "context"}]}}], "withdrawals": []})
    monkeypatch.setattr(pipeline, "retrieval", lambda *args: Retrieval())
    await pipeline.consolidate_pending("p")
    await pipeline.consolidate_pending("p", retry_failed=True)
    assert not pipeline.store("p").list(("insight",)) and len(model.calls) == 1
    assert "no supporting facts" in pipeline.store("p").jobs()[0]["error"]


@pytest.mark.asyncio
async def test_badcase_analysis_failures_are_not_future_diagnostic_samples(tmp_path):
    analyzer = BadCaseAnalyzer(None, str(tmp_path / "workspace"))
    analyzer.directory.mkdir(parents=True)
    (analyzer.directory / "failure.md").write_text(
        '---\nid: failed-analysis\nclassification: agent_execution\nfailure_kind: bad_case_analysis_failure\n---\n## 错误\n无效 JSON',
        encoding="utf-8")
    assert analyzer._load_cases("agent_execution") == []


@pytest.mark.asyncio
async def test_quota_badcase_persists_without_new_paid_diagnosis(tmp_path):
    class Trace:
        async def create_trace(self, *args, **kwargs): return "case"
        async def set_trace_operation(self, *args, **kwargs): pass
        async def append_event(self, *args, **kwargs): pass
        async def finish_trace(self, *args, **kwargs): pass

    class Analyzer:
        def classify(self, metadata): return "llm_api"
        def schedule(self, *args): raise AssertionError("quota failures must not schedule models")

    recorder = AgentBadCaseRecorder(Trace(), str(tmp_path / "workspace"), Analyzer())
    result = await recorder.capture(source_trace_id="trace", session_id="s", project_id="p",
        failure_kind="knowledge_retain_failure", actor="memory:retain", error="HTTP 402: Insufficient Balance")
    assert result == "case" and list(recorder.directory.glob("*.md"))


@pytest.mark.asyncio
async def test_context_tool_reply_uses_the_actual_assistant_call_id(tmp_path):
    class Tool:
        name = "GetTraceContext"
        def checkPermissions(self, *args): return PermissionResult.ALLOW
        async def execute(self, *args): return ToolResult(success=True, data={"evidence": "available"})

    model = Model('{}')
    analyzer = FileTraceAnalyzer(model, str(tmp_path), None)
    messages = []
    call = SimpleNamespace(tool_call_id="", tool_name="GetTraceContext", tool_input={})
    await analyzer._complete_analysis_tool_call(messages, "memory_summary_fallback", Tool(), call, "",
        project_id="p", session_id="s", source_trace_id="t", execution_events=None, tag=":offline")
    assert messages[0]["tool_calls"][0]["id"] == messages[1]["tool_call_id"]
    assert isinstance(messages[1]["content"], str)


@pytest.mark.asyncio
async def test_shared_agent_loop_pairs_multiple_missing_tool_ids():
    class LoopModel:
        def __init__(self): self.calls = []
        async def chat(self, **kwargs):
            self.calls.append(deepcopy(kwargs["messages"]))
            if len(self.calls) == 1:
                yield LLMResponse(type="tool_use", tool_name="Read", tool_input={"path": "a.md"})
                yield LLMResponse(type="tool_use", tool_name="Read", tool_input={"path": "b.md"})
            else:
                yield LLMResponse(type="text_delta", content="完成")
            yield LLMResponse(type="done")

    async def execute(*args): return True, "正文", ""
    model = LoopModel()
    events = [e async for e in query(model, position="sub_agent", messages=[{"role": "user", "content": "读取"}],
        system_prompt="", tools=[], max_turns=2, tag=":offline", execute_tool=execute)]
    sent = model.calls[-1]
    calls = next(m["tool_calls"] for m in sent if m.get("tool_calls"))
    replies = [m["tool_call_id"] for m in sent if m["role"] == "tool_result"]
    assert [c["id"] for c in calls] == replies and len(set(replies)) == 2
    assert events[-1]["final_text"] == "完成"


@pytest.mark.asyncio
async def test_input_budget_failure_never_sends_a_model_request(tmp_path):
    model = Model('{}')
    pipeline = KnowledgePipeline(model, tmp_path, None, "model", config={"analysis_input_tokens": 2})
    with pytest.raises(ValueError, match="input budget"):
        await pipeline._call("p", "大型正文" * 100, "retain")
    assert not model.calls


@pytest.mark.asyncio
async def test_analysis_trace_saves_cache_usage_and_token_total(tmp_path):
    class Trace:
        def __init__(self): self.events, self.tokens = [], 0
        async def create_trace(self, *args, **kwargs): return "trace"
        async def set_trace_operation(self, *args, **kwargs): pass
        async def append_event(self, *args, **kwargs): self.events.append((args[2], args[4]))
        async def finish_trace(self, *args): self.tokens = args[3]

    trace = Trace()
    pipeline = KnowledgePipeline(Model('{}'), tmp_path, None, "model", trace_store=trace)
    await pipeline._call("p", "只输出 JSON", "retain")
    assert trace.tokens == 8
    assert next(p for kind, p in trace.events if kind == "llm_usage")["prompt_cache_hit_tokens"] == 3


@pytest.mark.parametrize("text", ['```json\n{"ok":true}\n```', '{"value":"a } brace"}'])
def test_json_parser_reads_fences_and_braces_in_strings(text):
    assert json_object(text)


@pytest.mark.parametrize("text", ['{"ok":true}{"second":true}', '{"ok":', 'plain prose'])
def test_json_parser_never_guesses_incomplete_output(text):
    with pytest.raises(ValueError):
        json_object(text)
