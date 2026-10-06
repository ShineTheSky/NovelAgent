"""Prompt wiring and state boundaries; synthetic fixtures, no production DB/model calls."""

from copy import deepcopy
import json

import pytest

from novelagent.llm.client import LLMResponse
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.extraction_prompts import (
    MEMORY_RETAIN_PROMPT, MEMORY_MATCH_PROMPT, INSIGHT_CONSOLIDATE_PROMPT,
    ENTITY_EXTRACTION_RULES, ENTITY_IDENTITY_RULES,
)
from novelagent.memory.knowledge_store import fingerprint
from novelagent.memory.models import MemoryPackage, Entity


def package(completion="complete", event="ev"):
    return MemoryPackage.model_validate({
        "title": "世界魔力随副本而异", "direction": "魔力范围", "background": "旧设定把低魔推广到全书",
        "requirement": "仅首个副本低魔", "reason": "修正局部特性被扩大为全局的问题",
        "result": "用户明确新的世界规则", "completion": completion,
        "facts": [{"domain": "content", "claim": "低魔仅是首个副本特性", "modality": "asserted",
                   "source_event_ids": [event]}],
    }).model_dump()


def unit(status="failed"):
    return {"history_id": "h", "kind": "conversation", "status": status, "events": [
        {"event_id": "ev", "history_id": "h", "kind": "user_input", "content": "仅首个副本低魔"}],
        "selected_turns": []}


class Embedding:
    async def encode(self, texts):
        return [[1., 0.] for _ in texts]


class Model:
    def __init__(self, payload, decision=None):
        self.payload, self.decision, self.calls = payload, decision, []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = {"memories": self.payload} if kwargs["tag"].endswith("retain") else self.decision
        yield LLMResponse(type="text_delta", content=json.dumps(response))


def pipeline_for(tmp_path, model, monkeypatch, candidates=()):
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test-model")

    class Retrieval:
        async def recall(self, *args, **kwargs):
            assert kwargs["purpose"] == "memory_match"
            return {"status": "ok", "items": [{"record": r} for r in candidates]}

    async def no_learning(*args, **kwargs):
        pass

    monkeypatch.setattr(pipeline, "retrieval", lambda *args: Retrieval())
    monkeypatch.setattr(pipeline, "consolidate_pending", no_learning)
    return pipeline


@pytest.mark.parametrize("prompt", [MEMORY_RETAIN_PROMPT, MEMORY_MATCH_PROMPT], ids=["retain", "match"])
def test_entity_coverage_and_identity_rules_shared_by_retain_and_match(prompt):
    assert ENTITY_EXTRACTION_RULES in prompt and ENTITY_IDENTITY_RULES in prompt
    for kind in ("person", "organization", "place", "object", "event", "mechanism", "concept",
                 "theme", "technique", "goal", "criterion", "artifact", "agent", "user"):
        assert kind + "=" in ENTITY_EXTRACTION_RULES
    for boundary in ("不要把事实包内全部实体复制", "不能自动添加 user", "不自动等于当前用户",
                     "entities=[]", "实体出现不代表认同"):
        assert boundary in prompt
    assert "不能静默换身份或 kind" in MEMORY_MATCH_PROMPT
    assert "短句”不等于“快节奏" in prompt and "代词只有在上下文唯一指向" in prompt


def test_entity_schema_guides_new_types_without_rejecting_legacy_types():
    schema = Entity.model_json_schema()["properties"]
    assert "稳定称呼" in schema["name"]["description"]
    assert "concept/theme/technique" in schema["kind"]["description"]
    assert "上下位关系" in schema["aliases"]["description"]
    assert Entity(name="林舟", kind="character").kind == "character"
    assert Entity(name="旧地点", kind="location").kind == "location"


@pytest.mark.asyncio
async def test_content_and_writing_entities_persist_per_fact_with_shared_identity(tmp_path, monkeypatch):
    payload = package()
    payload.update(title="陈林入门场景的心理表达", direction="入门场景心理描写",
                   background="陈林加入青云门", requirement="心理描写更自然", reason="", result="用户明确表达目标")
    person = {"name": "陈林", "kind": "person", "entity_id": "new:entity:1"}
    payload["facts"] = [
        {"domain": "content", "claim": "陈林加入青云门", "source_event_ids": ["ev"],
         "entities": [deepcopy(person), {"name": "青云门", "kind": "organization"}]},
        {"domain": "writing", "claim": "用户希望陈林的心理描写更自然", "source_event_ids": ["ev"],
         "entities": [{"name": "user", "kind": "user"}, deepcopy(person),
                      {"name": "心理描写", "kind": "technique"}, {"name": "更自然", "kind": "goal"}]},
    ]
    model = Model([payload])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    source = unit()
    source["events"][0]["content"] = "陈林加入青云门；我希望陈林的心理描写更自然。"
    await pipeline.process("p", source)
    saved = pipeline.store("p").list(("memory",))[0]["payload"]["facts"]
    a, b = [{entity["name"]: entity for entity in fact["entities"]} for fact in saved]
    assert a["陈林"]["entity_id"] == b["陈林"]["entity_id"]
    assert "user" not in a and b["user"]["kind"] == "user"
    assert "青云门" not in b and b["心理描写"]["kind"] == "technique"
    with pipeline.store("p").connect() as db:
        assert db.execute("SELECT count(*) FROM entities").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM entity_links").fetchone()[0] == 6
    assert ENTITY_EXTRACTION_RULES in model.calls[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_entity_contract_change_does_not_reuse_previous_prepared(tmp_path, monkeypatch):
    model = Model([package()])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    source = unit()
    evidence, artifacts = pipeline._evidence("p", source)
    old_digest = fingerprint({"events": evidence, "artifacts": artifacts, "selected_turns": [],
                              "extraction_contract": "2026-10-06-trusted-missing-fragments-v6"})
    pipeline.store("p").input_state("h", old_digest, prepared={"memories": [package("partial")]}, status="complete")
    await pipeline.process("p", source)
    await pipeline.process("p", source)
    assert len(model.calls) == 1
    assert pipeline.store("p").list(("memory",))[0]["payload"]["completion"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,completion", [
    ("failed", "complete"), ("interrupted", "no_change"), ("completed", "partial"), ("failed", "unknown"),
])
async def test_execution_state_does_not_override_business_completion(tmp_path, monkeypatch, status, completion):
    model = Model([package(completion)])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    await pipeline.process("p", unit(status))
    current = pipeline.store("p").list(("memory",))[0]
    assert current["payload"]["completion"] == completion
    assert current["payload"]["unresolved"] == ""
    prompt = model.calls[0]["messages"][-1]["content"]
    assert prompt.startswith(MEMORY_RETAIN_PROMPT) and "此前创作、审阅、润色均已结束" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["new", "update"])
async def test_matching_receives_source_evidence_and_preserves_completion(tmp_path, monkeypatch, action):
    model = Model([package()])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    store = pipeline.store("p")
    previous = store.save_memory(package(event="old"), [{"event_id": "old", "history_id": "prior",
        "kind": "user_input", "content": "首个副本为低魔"}], [1, 0], "test-model")
    pipeline = pipeline_for(tmp_path, model, monkeypatch, [previous])
    merged = deepcopy(previous["payload"]) if action == "update" else package()
    merged["completion"] = "complete"
    model.decision = {"action": action, "memory_id": previous["id"] if action == "update" else "",
                      "expected_version": 1 if action == "update" else 0, "memory": merged}
    await pipeline.process("p", unit())
    records = store.list(("memory",))
    assert all(record["payload"]["completion"] == "complete" for record in records)
    prompt = model.calls[1]["messages"][-1]["content"]
    assert prompt.startswith(MEMORY_MATCH_PROMPT)
    evidence_json, candidates_json = prompt.split("\nNewEvidence:", 1)[1].split("\nCandidates:", 1)
    assert json.loads(evidence_json)[0]["event_id"] == "ev"
    assert json.loads(candidates_json)[0]["evidence"][0]["event_id"] == "old"
    if action == "update":
        assert store.get(previous["id"])["version"] == 2


@pytest.mark.asyncio
async def test_old_prepared_extraction_is_not_reused_and_new_contract_is_idempotent(tmp_path, monkeypatch):
    model = Model([package()])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    source = unit()
    evidence, artifacts = pipeline._evidence("p", source)
    old_digest = fingerprint({"events": evidence, "artifacts": artifacts, "selected_turns": []})
    pipeline.store("p").input_state("h", old_digest, prepared={"memories": [package("partial")]}, status="complete")
    await pipeline.process("p", source)
    await pipeline.process("p", source)
    assert len(model.calls) == 1
    assert pipeline.store("p").list(("memory",))[0]["payload"]["completion"] == "complete"


@pytest.mark.asyncio
async def test_legal_empty_extraction_creates_no_knowledge(tmp_path, monkeypatch):
    model = Model([])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    source = unit()
    source["events"] = [{"event_id": "error", "history_id": "h", "kind": "tool_failure", "content": "HTTP 400"}]
    assert await pipeline.process("p", source)
    assert pipeline.store("p").list() == [] and pipeline.store("p").jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [False, True])
async def test_retain_and_match_bind_versions_and_keep_fragment_evidence(tmp_path, monkeypatch, matching):
    from novelagent.history import DocumentHistoryStore
    history = DocumentHistoryStore(tmp_path / "p")
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "v1", None, "旧正文", "旧正文")
    history.record_revision(path, "v2", "v1", "新正文", "新正文")
    source = unit("completed")
    source["events"].append({"event_id": "edit", "history_id": "h", "kind": "file_change_attempt",
        "content": json.dumps({"tool": "Edit", "success": True, "params": {"path": "world/a.md",
            "expected_revision_id": "v1", "old_string": "旧正文", "new_string": "新正文"}, "result": "当前修订: v2"})})
    raw = package()
    raw.update(artifact_paths=["world/a.md"], fragments=[{"source_event_id": "edit", "before": "旧正文", "after": "新正文"}])
    model = Model([raw])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    if matching:
        prior = pipeline.store("p").save_memory(package(), source["events"][:1], [1, 0], "test-model")
        raw["facts"][0]["fact_id"] = prior["payload"]["facts"][0]["fact_id"]
        model.decision = {"action": "update", "memory_id": prior["id"], "expected_version": 1, "memory": raw}
        pipeline = pipeline_for(tmp_path, model, monkeypatch, [prior])
    await pipeline.process("p", source)
    saved = pipeline.store("p").list(("memory",))[0]
    fragment = saved["payload"]["fragments"][0]
    assert fragment["revision_id"] == "v2" and fragment["previous_revision_id"] == "v1"
    assert fragment["source_event_id"] == "edit"
    assert "edit" in {e["event_id"] for e in saved["evidence"]}
    assert saved["version"] == (2 if matching else 1)
    for call in model.calls:
        prompt = call["messages"][-1]["content"]
        schema = json.loads(prompt.split("\nSchema:", 1)[1].split("\nHistory:", 1)[0].split("\nNew:", 1)[0])
        assert set(schema["$defs"]["FragmentSelection"]["properties"]) == {"source_event_id", "before", "after"}


class RepairModel:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        yield LLMResponse(type="text_delta", content=json.dumps(self.responses[min(len(self.calls)-1, len(self.responses)-1)]))


@pytest.mark.asyncio
async def test_equivalent_quote_needs_one_call_and_keeps_evidence(tmp_path, monkeypatch):
    raw = package()
    raw["source_text"] = '仅首个副本“低魔”'
    source = unit("completed")
    source["events"][0]["content"] = '仅首个副本\\"低魔\\"'
    model = Model([raw])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    await pipeline.process("p", source)
    assert len(model.calls) == 1
    saved = pipeline.store("p").list(("memory",))[0]
    assert saved["payload"]["source_text"] == raw["source_text"]
    assert saved["evidence"][0]["content"] == source["events"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [True, False])
async def test_bad_case_records_each_validation_even_if_recovered(tmp_path, monkeypatch, recovered):
    wrong = package()
    wrong["facts"][0]["source_event_ids"] = ["missing"]
    model = RepairModel([{"memories": [wrong]}, {"memories": [package()]}] if recovered else [{"memories": [wrong]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    pipeline.config["analysis_max_turns"] = 2

    class Recorder:
        def __init__(self):
            self.calls = []

        async def capture(self, **kwargs):
            self.calls.append(kwargs)

    recorder = Recorder()
    pipeline.bad_cases = recorder
    if recovered:
        await pipeline.process("p", unit("completed"))
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["params"]["error_kind"] == "validation"
        assert recorder.calls[0]["params"]["location"] == "memories[0].facts[0].source_event_ids"
    else:
        with pytest.raises(RuntimeError, match="max_turns=2"):
            await pipeline.process("p", unit("completed"))
        assert len(recorder.calls) == 3
        assert [c["params"]["error_kind"] for c in recorder.calls] == ["validation", "validation", "execution"]
        assert recorder.calls[0]["failure_kind"] == "knowledge_retain_failure"
    assert all(c["schedule_analysis"] is False and c["params"]["history_id"] == "h" for c in recorder.calls)
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_missing_fragment_original_is_saved_without_repair_call(tmp_path, monkeypatch):
    from novelagent.history import DocumentHistoryStore
    source = unit("completed")
    source["events"].append({"event_id": "edit", "history_id": "h", "kind": "file_change_attempt",
        "content": json.dumps({"tool": "Edit", "success": True,
            "params": {"path": "world/a.md", "expected_revision_id": "old"}, "result": "当前修订: new"})})
    raw = package()
    raw.update(artifact_paths=["world/a.md"], fragments=[{"source_event_id": "edit", "before": "旧正文", "after": "新正文"}])
    model = Model([raw])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    await pipeline.process("p", source)
    assert len(model.calls) == 1
    fragment = pipeline.store("p").list(("memory",))[0]["payload"]["fragments"][0]
    assert fragment["verification"] == "model_trusted"
    assert fragment["before"] == "旧正文" and fragment["after"] == "新正文"
    assert DocumentHistoryStore(tmp_path / "p").get_revision("new") is None


@pytest.mark.asyncio
async def test_recovered_error_writes_bad_case_file_without_model_analysis(tmp_path, monkeypatch):
    from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
    class Trace:
        def __init__(self): self.events = []
        async def create_trace(self, *args, **kwargs): return "case"
        async def set_trace_operation(self, *args): pass
        async def append_event(self, *args, **kwargs): self.events.append(args)
        async def finish_trace(self, *args): pass
    class Analyzer:
        def classify(self, metadata): return "agent_execution"
        def schedule(self, *args): raise AssertionError("Bad Case recording must not call another model")
    trace = Trace()
    recorder = AgentBadCaseRecorder(trace, str(tmp_path / "workspace"), Analyzer())
    wrong = package()
    wrong["facts"][0]["source_event_ids"] = ["missing"]
    model = RepairModel([{"memories": [wrong]}, {"memories": [package()]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    pipeline.bad_cases = recorder
    await pipeline.process("p", unit("completed"))
    files = list((tmp_path / "data/agent_bad_cases").glob("*.md"))
    assert len(files) == 1
    assert "missing" in files[0].read_text(encoding="utf-8")
    context = trace.events[0][-1]
    assert context["params"]["history_id"] == "h"
    assert context["params"]["location"] == "memories[0].facts[0].source_event_ids"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_http_error_records_once_across_call_and_process(tmp_path, monkeypatch):
    class ModelError:
        async def chat(self, **kwargs):
            yield LLMResponse(type="error", error="HTTP 500")
    class Recorder:
        def __init__(self): self.calls = []
        async def capture(self, **kwargs):
            self.calls.append(kwargs)
            return "case"
    pipeline = pipeline_for(tmp_path, ModelError(), monkeypatch)
    recorder = Recorder()
    pipeline.bad_cases = recorder
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await pipeline.process("p", unit("completed"))
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["params"]["error_kind"] == "execution"
    assert recorder.calls[0]["schedule_analysis"] is False


def editable_unit(tmp_path):
    from novelagent.history import DocumentHistoryStore
    history = DocumentHistoryStore(tmp_path / "p")
    path = tmp_path / "p" / "world" / "a.md"
    history.record_revision(path, "v1", None, "旧正文", "旧正文")
    history.record_revision(path, "v2", "v1", "新正文", "新正文")
    source = unit("completed")
    source["events"].append({"event_id": "edit", "history_id": "h", "kind": "file_change_attempt",
        "content": json.dumps({"tool": "Edit", "success": True, "params": {"path": "world/a.md",
            "expected_revision_id": "v1", "old_string": "旧正文", "new_string": "新正文"}, "result": "当前修订: v2"})})
    raw = package()
    raw.update(artifact_paths=["world/a.md"], fragments=[{"source_event_id": "edit", "before": "旧正文", "after": "新正文"}])
    return source, raw


@pytest.mark.asyncio
async def test_wrong_fragment_is_explained_and_repaired_in_same_context(tmp_path, monkeypatch):
    source, correct = editable_unit(tmp_path)
    wrong = deepcopy(correct)
    wrong["fragments"][0]["before"] = "另一处修改的片段"
    model = RepairModel([{"memories": [wrong]}, {"memories": [correct]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    await pipeline.process("p", source)
    assert len(model.calls) == 2
    second = model.calls[1]["messages"]
    assert second[:len(model.calls[0]["messages"])] == model.calls[0]["messages"]
    assert json.loads(second[-2]["content"])["memories"][0] == wrong
    error = json.loads(second[-1]["content"].split("\n校验详情：", 1)[1])
    assert error["location"] == "memories[0].fragments[0].before"
    assert error["source_event_id"] == "edit" and error["path"] == "world/a.md"
    assert error["supplied_text"] == "另一处修改的片段"
    assert error["expected_source_field"] == "params.old_string" and error["event_text"] == "旧正文"
    assert error["correction"] and model.calls[0]["tools"] == model.calls[1]["tools"]
    assert pipeline.store("p").list(("memory",))[0]["payload"]["fragments"][0]["revision_id"] == "v2"


@pytest.mark.asyncio
async def test_model_version_field_reports_exact_schema_location_before_repair(tmp_path, monkeypatch):
    source, correct = editable_unit(tmp_path)
    wrong = deepcopy(correct)
    wrong["fragments"][0]["previous_revision_id"] = "guessed"
    model = RepairModel([{"memories": [wrong]}, {"memories": [correct]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    await pipeline.process("p", source)
    details = json.loads(model.calls[1]["messages"][-1]["content"].split("\n校验详情：", 1)[1])
    assert details["location"] == "memories[0]"
    assert details["errors"][0]["location"] == ["fragments", 0, "previous_revision_id"]
    assert details["errors"][0]["message"] == "Extra inputs are not permitted"


@pytest.mark.asyncio
async def test_invalid_output_only_fails_after_max_turns_and_is_not_cached(tmp_path, monkeypatch):
    source, correct = editable_unit(tmp_path)
    correct["fragments"][0]["before"] = "错误片段"
    model = RepairModel([{"memories": [correct]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    pipeline.config["analysis_max_turns"] = 3
    with pytest.raises(RuntimeError, match="max_turns=3"):
        await pipeline.process("p", source)
    assert len(model.calls) == 3
    assert all("校验详情" in c["messages"][-1]["content"] for c in model.calls[1:])
    store = pipeline.store("p")
    assert store.list() == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_results").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_invalid_cached_response_is_repaired_not_returned(tmp_path):
    model = RepairModel([{"ok": False}, {"ok": True}])
    pipeline = KnowledgePipeline(model, tmp_path, None, "test", config={"analysis_max_turns": 2})
    assert await pipeline._call("p", "提取", "match") == {"ok": False}
    def validate(result):
        if not result["ok"]:
            raise ValueError("ok 应为 true，实际为 false")
    assert await pipeline._call("p", "提取", "match", validate=validate) == {"ok": True}
    assert len(model.calls) == 2
    assert "ok 应为 true，实际为 false" in model.calls[1]["messages"][-1]["content"]
    assert await pipeline._call("p", "提取", "match", validate=validate) == {"ok": True}
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_match_validation_feedback_repairs_candidate_version(tmp_path, monkeypatch):
    old = package()
    model = RepairModel([])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    prior = pipeline.store("p").save_memory(old, unit()["events"], [1, 0], "test-model")
    correct = {"action": "update", "memory_id": prior["id"], "expected_version": 1,
               "memory": prior["payload"]}
    wrong = {**correct, "expected_version": 999}
    model.responses = [{"memories": [old]}, wrong, correct]
    pipeline = pipeline_for(tmp_path, model, monkeypatch, [prior])
    await pipeline.process("p", unit("completed"))
    assert len(model.calls) == 3
    details = json.loads(model.calls[2]["messages"][-1]["content"].split("\n校验详情：", 1)[1])
    assert details["location"] == "memory_id/expected_version" and details["supplied_version"] == 999
    assert details["candidates"] == [{"id": prior["id"], "version": 1}]
    assert pipeline.store("p").get(prior["id"])["version"] == 2


@pytest.mark.asyncio
async def test_insight_prompt_uses_memory_sources_and_keeps_authority_boundaries(tmp_path, monkeypatch):
    model = Model([], {"insights": [], "withdrawals": []})
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test-model")
    memory = pipeline.store("p").save_memory(package(), unit()["events"], [1, 0], "test-model")

    class Retrieval:
        async def recall(self, *args, **kwargs):
            return {"status": "ok", "items": []}

    monkeypatch.setattr(pipeline, "retrieval", lambda *args: Retrieval())
    await pipeline.consolidate_pending("p")
    prompt = model.calls[0]["messages"][-1]["content"]
    assert prompt.startswith(INSIGHT_CONSOLIDATE_PROMPT)
    assert "旧提取错误" in prompt and "原始事实”当成认可派生解释" in prompt
    memories_json = prompt.split("\nMemories:", 1)[1].split("\nExisting:", 1)[0]
    sources = json.loads(memories_json)
    assert sources[0]["id"] == memory["id"] and sources[0]["version"] == 1
    assert sources[0]["payload"]["facts"][0]["source_event_ids"] == ["ev"]
    assert not pipeline.store("p").jobs()


@pytest.mark.parametrize("section,requirements", [
    ("内容域：提取可持续约束作品的知识", [
        "生效条件、代价、上限、禁止事项", "前置条件", "人物知道与不知道的信息",
        "不得改变的设定、关系、事件顺序", "旧口径到新口径", "何时算违反",
        "临时状态和普通情节记录留在 Memory", "限定有效范围",
    ]),
    ("撰写域：提取能执行、能检查的创作知识", [
        "场景、情节步骤或信息释放安排", "视角控制", "感知动作心理的承接",
        "实际采用的改法", "二次反馈和取舍", "违规的具体表现", "需对照的前后文",
        "来源未讨论的写法或标准不能凭常识自动补入", "不编具体方法",
    ]),
], ids=["content", "writing"])
def test_insight_prompt_has_concrete_domain_and_purpose_criteria(section, requirements):
    block = INSIGHT_CONSOLIDATE_PROMPT.split(f"【{section}】", 1)[1].split("\n【", 1)[0]
    for purpose in ("指导规划", "指导写作", "指导润色", "指导审查"):
        assert purpose + "：" in block
    for requirement in requirements:
        assert requirement in block


def test_insight_prompt_classifies_without_cloning_or_unfounded_generalization():
    for boundary in (
        "不能按来源 Agent 或使用 Agent 决定知识域",
        "在 purpose 中写明用途及要解决的具体问题",
        "这是现有字段的分类，不新增字段或类型",
        "不是所有审查用途都必须另建 standard",
        "同一个 Memory 可以产生多个独立用途、独立结论的 Insight",
        "来源的 domain 不机械限制结论的 knowledge_domains",
        "不能只为规划、写作、润色、审查各复制一份相同知识",
        "同一条 content Insight 的 claim/check",
        "跨人物／场景复用", "才另提 writing/standard",
        "solutions 中，不拆成互相竞争的独立 Insight",
        "不能归纳为全书禁用短句", "多个用途不意味着人类确认",
    ):
        assert boundary in INSIGHT_CONSOLIDATE_PROMPT
