"""No paid model calls: real SQLite/USearch and deterministic analysis responses."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from tests.conftest import quoted_memory_response

from novelagent.memory.models import MemoryPackage, Insight, chosen_solution, time_key
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.retrieval import KnowledgeRetrieval
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.runtime import KnowledgeRuntime
from novelagent.tools.search_memory import SearchMemoryTool
from novelagent.tools.base import ToolContext


class Embedding:
    async def encode(self, texts):
        return np.asarray([[1., 0., 0.] for _ in texts], dtype=np.float32)


def package(title="自然承接", **overrides):
    raw = {"title": title, "direction": title, "background": "前段为快速动作", "requirement": "本段恢复舒缓",
           "statements": [{"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": title, "source_event_ids": ["ev1"]}]}
    raw.update(overrides)
    return MemoryPackage.model_validate(raw).model_dump()


def evidence():
    return [{"event_id": "ev1", "history_id": "h1", "kind": "user_input", "content": "本段恢复舒缓"}]


def save(store, title="自然承接", **overrides):
    return store.save_memory(package(title, **overrides), evidence(), [1., 0., 0.], "model")


def learned(memory, **overrides):
    raw = {"title": "节奏转换", "claim": "依场景调整节奏", "kind": "method", "classifications": [{"domain": "writing", "categories": ["polishing"]}], "purpose": "自然承接",
           "method": "动作之后增加感知承接", "sources": [{"memory_id": memory["id"], "version": memory["version"],
           "statement_ids": [memory["payload"]["statements"][0]["statement_id"]]}]}
    raw.update(overrides)
    return Insight.model_validate(raw).model_dump()


def publish(store, memory, **overrides):
    return store.publish_insights([{"payload": learned(memory, **overrides), "evidence": evidence(),
        "vector": [1., 0., 0.], "model_id": "model"}], {memory["id"]: memory["version"]})[0]


def test_immutable_versions_and_idempotency(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = save(store)
    payload = deepcopy(first["payload"])
    payload["result"] = "恢复舒缓"
    second = store.save_memory(payload, evidence(), [1, 0, 0], "model", first["id"], 1, "operation")
    assert second["version"] == 2 and second["previous_version"] == 1
    assert store.get(first["id"], 1)["payload"]["result"] == "未知"
    assert store.save_memory(payload, [], [0, 0, 0], "model", operation_id="operation")["id"] == first["id"]
    with pytest.raises(ValueError, match="version conflict"):
        store.save_memory(payload, evidence(), [1, 0, 0], "model", first["id"], 1)
    assert store.jobs() == [{"id": first["id"], "version": 2, "status": "pending", "error": ""}]


def test_invalid_relation_rolls_back(tmp_path):
    store = KnowledgeStore(tmp_path)
    with pytest.raises(ValueError, match="target not found"):
        save(store, relations=[{"kind": "semantic", "predicate": "related", "target_id": "missing",
                               "target_version": 1, "source_event_ids": ["ev1"]}])
    assert store.list() == [] and store.jobs() == []


def test_publication_keeps_old_knowledge_until_atomic_switch(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = save(store)
    insight = publish(store, first)
    second = store.save_memory(first["payload"], evidence(), [1, 0, 0], "model", first["id"], 1)
    assert store.get(insight["id"])["payload"]["sources"][0]["version"] == 1
    assert any(t["id"] == first["id"] and t["version"] == 1 for t in store.graph()["times"])
    with pytest.raises(ValueError, match="changed"):
        store.publish_insights([], {first["id"]: 1}, [insight["id"]])
    assert store.get(insight["id"])["status"] == "active"
    store.publish_insights([{"id": insight["id"], "expected_version": 1, "payload": learned(second),
        "evidence": evidence(), "vector": [1, 0, 0], "model_id": "model"}], {first["id"]: 2})
    assert store.get(insight["id"])["version"] == 2 and not store.jobs()


@pytest.mark.asyncio
async def test_four_routes_are_audited_and_hard_time_filter_is_numeric(tmp_path):
    store = KnowledgeStore(tmp_path)
    def fact(pos):
        return [{"classifications": [{"domain": "content", "categories": ["setting"]}], "claim": "林舟节奏", "source_event_ids": ["ev1"],
                 "entities": [{"name": "林舟", "kind": "person", "aliases": ["阿舟"]}],
                 "times": [{"dimension": "chapter", "start": pos}]}]
    one = save(store, "节奏一", statements=fact("1.2.1"))
    two = save(store, "节奏二", statements=fact("1.12.1"))
    retrieval = KnowledgeRetrieval(store, Embedding(), "model")
    result = await retrieval.recall("阿舟节奏 1.2.1", purpose="memory_match", temporal={
        "dimension": "chapter", "start": "1.2.0", "end": "1.2.999999", "hard": True})
    assert result["status"] == "ok"
    assert [r["record"]["id"] for r in result["items"]] == [one["id"]]
    assert set(result["routes"]) == {"semantic", "keyword", "relation", "temporal"}
    assert all(r["count"] > 0 for r in result["routes"].values())
    assert retrieval._index is not None  # real USearch HNSW, not a semantic linear scan
    assert store.get(two["id"])["version"] == 1


@pytest.mark.asyncio
async def test_reverse_causal_traversal_from_entity_seed(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = save(store, "甲", statements=[{"classifications": [{"domain": "content", "categories": ["setting"]}], "claim": "甲事件", "source_event_ids": ["ev1"],
                                     "entities": [{"name": "源角色"}]}])
    second = save(store, "乙", relations=[{"kind": "causal", "predicate": "caused_by",
        "target_id": first["id"], "target_version": 1, "source_event_ids": ["ev1"], "source_statement_id": "new:1",
        "target_statement_id": first["payload"]["statements"][0]["statement_id"]}], statements=[
            {"statement_id": "new:1", "classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": "乙结果", "source_event_ids": ["ev1"]}])
    result = await KnowledgeRetrieval(store, Embedding(), "model").recall("源角色", purpose="memory_match")
    row = next(i for i in result["items"] if i["record"]["id"] == second["id"])
    assert "relation" in row["ranks"] and any("causal" in p for p in row["paths"])


@pytest.mark.asyncio
async def test_failures_are_not_empty_results(tmp_path):
    store = KnowledgeStore(tmp_path)
    assert (await KnowledgeRetrieval(store, Embedding(), "model").recall("x"))["status"] == "empty"
    save(store)
    result = await KnowledgeRetrieval(store, Embedding(), "changed").recall("x", purpose="memory_match")
    assert result["status"] == "failed" and "model changed" in result["error"]
    class Slow:
        async def encode(self, texts):
            await asyncio.sleep(.1)
    result = await KnowledgeRetrieval(store, Slow(), "model", timeout=.005).recall("x", purpose="memory_match")
    assert result["status"] == "timeout"


def test_solution_selection_and_review_scope(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store)
    knowledge = publish(store, memory, authority="reviewer", roles=["reviewer"], solutions=[
        {"solution_id": "a", "explanation": "改向", "score": .9},
        {"solution_id": "b", "explanation": "上次过度", "score": .6, "human_selected": True}])
    assert chosen_solution(knowledge["payload"])["solution_id"] == "b"
    assert not KnowledgeRetrieval.applicable(knowledge, "chapter_writer")
    assert KnowledgeRetrieval.applicable(knowledge, "reviewer")
    local = {**knowledge, "payload": {**knowledge["payload"], "artifact_paths": ["chapters/a.md"]}}
    assert not KnowledgeRetrieval.applicable(local, "reviewer")
    assert KnowledgeRetrieval.applicable(local, "reviewer", ["chapters/a.md"])


@pytest.mark.asyncio
async def test_actual_context_usage_deduplicates_and_promotes(tmp_path):
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "model")
    store = pipeline.store("p")
    memory = save(store)
    insight = publish(store, memory)
    runtime = KnowledgeRuntime(pipeline, {"pattern_after_uses": 2})
    context = await runtime.assemble("p", "节奏")
    assert context["attachments"][0]["id"] == insight["id"]
    runtime.mark_used("p", context["attachments"], [], "t1", "main")
    assert store.get(insight["id"])["layer"] == "insight"
    messages = [{"role": "user", "content": context["text"]}]
    runtime.mark_used("p", context["attachments"], messages, "t1", "main")
    runtime.mark_used("p", context["attachments"], messages, "t1", "reviewer")
    assert store.get(insight["id"])["layer"] == "insight"
    runtime.mark_used("p", context["attachments"], messages, "t2", "main")
    assert store.get(insight["id"])["layer"] == "pattern"
    context = await runtime.assemble("p", "无关内容")
    assert context["attachments"][0]["layer"] == "pattern"


@pytest.mark.asyncio
async def test_runtime_failure_is_fail_open_and_records_case(tmp_path):
    cases = []
    class Cases:
        async def capture(self, **kwargs):
            cases.append(kwargs)
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "changed", bad_cases=Cases())
    memory = save(pipeline.store("p"))
    publish(pipeline.store("p"), memory)
    context = await KnowledgeRuntime(pipeline).assemble("p", "节奏")
    assert not context["text"] and context["recall"]["status"] == "failed" and cases


def test_chapter_and_date_normalization():
    assert time_key("chapter", "1.2.9") < time_key("chapter", "1.12.1")
    assert time_key("occurred", "2026-10-04T08:00:00+08:00") == time_key("occurred", "2026-10-04")
    with pytest.raises(ValueError):
        time_key("chapter", "1.2")


@pytest.mark.asyncio
async def test_explicit_memory_version_read_does_not_change_layer(tmp_path):
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "model")
    store = pipeline.store("p")
    first = save(store)
    store.save_memory(first["payload"], evidence(), [1, 0, 0], "model", first["id"], 1)
    tool = SearchMemoryTool(pipeline)
    result = await tool.execute({"memory_id": first["id"], "version": 1}, ToolContext("s", "p", str(tmp_path/"p")))
    assert result.success and json.loads(result.data)[0]["version"] == 1
    assert store.get(first["id"])["layer"] == "memory"


@pytest.mark.asyncio
async def test_history_retains_only_memory_then_independent_insight(tmp_path):
    class LLM:
        def __init__(self):
            self.tags = []
        async def chat(self, **kwargs):
            self.tags.append(kwargs["tag"])
            if kwargs["tag"].endswith("retain"):
                response = {"memories": [package()]}
            else:
                memory = pipeline.store("p").list(("memory",))[0]
                response = {"insights": [{"payload": learned(memory)}], "withdrawals": []}
            yield SimpleNamespace(type="text_delta", content=json.dumps(quoted_memory_response(response, kwargs['messages'])))
    llm = LLM()
    pipeline = KnowledgePipeline(llm, tmp_path, Embedding(), "model")
    unit = {"history_id": "h1", "kind": "conversation", "status": "completed", "events": evidence(), "selected_turns": []}
    await pipeline.process("p", unit)
    assert llm.tags == [":knowledge/retain", ":knowledge/consolidate"]
    await pipeline.process("p", unit)
    assert len(llm.tags) == 2
    store = pipeline.store("p")
    assert len(store.list(("memory",))) == 1 and len(store.list(("insight",))) == 1 and not store.jobs()


@pytest.mark.asyncio
async def test_failed_learning_keeps_memory_for_retry(tmp_path):
    class LLM:
        async def chat(self, **kwargs):
            if kwargs["tag"].endswith("retain"):
                yield SimpleNamespace(type="text_delta", content=json.dumps(quoted_memory_response({"memories": [package()]}, kwargs['messages'])))
            else:
                yield SimpleNamespace(type="error", error="interrupted")
    pipeline = KnowledgePipeline(LLM(), tmp_path, Embedding(), "model")
    unit = {"history_id": "h1", "kind": "conversation", "status": "completed", "events": evidence()}
    await pipeline.process("p", unit)
    store = pipeline.store("p")
    assert store.list(("memory",)) and not store.list(("insight",)) and store.jobs()[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_same_direction_updates_memory_and_direct_insight(tmp_path):
    class LLM:
        async def chat(self, **kwargs):
            tag = kwargs["tag"]
            if tag.endswith("retain"):
                response = {"memories": [package(statements=[{"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": "恢复舒缓",
                                                       "source_event_ids": ["ev2"]}])]}
            elif tag.endswith("match"):
                prior = pipeline.store("p").list(("memory",))[0]
                merged = deepcopy(prior["payload"])
                merged["result"] = "再次调整后自然衔接"
                merged["statements"].append({"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": "再次恢复舒缓", "source_event_ids": ["ev2"]})
                response = {"action": "update", "memory_id": prior["id"], "expected_version": prior["version"], "memory": merged}
            else:
                current = pipeline.store("p").get(memory["id"])
                response = {"insights": [{"id": insight["id"], "expected_version": 1, "payload": learned(current)}], "withdrawals": []}
            yield SimpleNamespace(type="text_delta", content=json.dumps(quoted_memory_response(response, kwargs['messages'])))
    pipeline = KnowledgePipeline(LLM(), tmp_path, Embedding(), "model")
    store = pipeline.store("p")
    memory = save(store)
    insight = publish(store, memory)
    unit = {"history_id": "h2", "kind": "conversation", "status": "completed", "events": [
        {"event_id": "ev2", "history_id": "h2", "kind": "user_input", "content": "再舒缓一些"}]}
    await pipeline.process("p", unit)
    current = store.get(memory["id"])
    assert current["version"] == 2 and len(store.list(("memory",))) == 1
    assert {e["history_id"] for e in current["evidence"]} == {"h1", "h2"}
    assert current["payload"]["statements"][0]["statement_id"] == memory["payload"]["statements"][0]["statement_id"]
    assert store.get(insight["id"])["payload"]["sources"][0]["version"] == 2


@pytest.mark.asyncio
async def test_partial_facts_and_supplemented_history_update(tmp_path):
    from novelagent.history import DocumentHistoryStore
    from novelagent.history.analyzer import HistoryAnalyzer
    class LLM:
        async def chat(self, **kwargs):
            if kwargs["tag"].endswith("retain"):
                response = {"memories": [package(statements=[{"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": "用户要求舒缓",
                    "source_event_ids": [event_id]}], completion="partial", unresolved="正文尚未调整")]}
            elif kwargs["tag"].endswith("match"):
                prior = pipeline.store("p").list(("memory",))[0]
                response = {"action": "update", "memory_id": prior["id"], "expected_version": 1,
                            "memory": {**prior["payload"], "completion": "complete", "result": "已恢复"}}
            else:
                response = {"insights": [], "withdrawals": []}
            yield SimpleNamespace(type="text_delta", content=json.dumps(quoted_memory_response(response, kwargs['messages'])))
    history = DocumentHistoryStore(tmp_path/"p")
    history.start_request("r", "s", "恢复舒缓")
    history.close_request("r", "interrupted", "尚未写入", session_turn_no=1)
    history_id = history.list_analyzable(include_partial=True)[0]
    event_id = next(e["event_id"] for e in history.get_unit(history_id)["events"] if e["kind"] == "user_input")
    assert not history.list_analyzable()
    llm = LLM()
    pipeline = KnowledgePipeline(llm, tmp_path, Embedding(), "model")
    analyzer = HistoryAnalyzer(llm, str(tmp_path), None, knowledge_pipeline=pipeline)
    await analyzer.analyze("p", history_id)
    memory = pipeline.store("p").list(("memory",))[0]
    assert memory["payload"]["completion"] == "partial"
    history.append_request_event("r", "main_output", "已恢复", event_id="later")
    assert history.get_unit(history_id)["analysis_status"] == "pending"
    history.close_request("r", "completed", "已恢复", session_turn_no=1)
    await analyzer.analyze("p", history_id)
    assert pipeline.store("p").get(memory["id"])["version"] == 2


def test_200_title_window_does_not_archive_packages(tmp_path):
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "model")
    for i in range(201):
        save(pipeline.store("p"), f"方向{i}")
    assert KnowledgeRuntime(pipeline).index("p").count("\n- ") == 200
    assert len(pipeline.store("p").list(("memory",))) == 201


def test_learning_one_source_does_not_complete_other_source_job(tmp_path):
    store = KnowledgeStore(tmp_path)
    one, two = save(store, "一"), save(store, "二")
    store.publish_insights([], {one["id"]: 1, two["id"]: 1}, completed_jobs={one["id"]: 1})
    assert [j["id"] for j in store.jobs()] == [two["id"]]


@pytest.mark.asyncio
async def test_temporal_route_can_expand_without_semantic_or_keyword_hits(tmp_path):
    class Orthogonal:
        async def encode(self, texts):
            return [[0, 1, 0] for _ in texts]
    store = KnowledgeStore(tmp_path)
    one = save(store, "甲", statements=[{"classifications": [{"domain": "content", "categories": ["setting"]}], "claim": "事件甲", "source_event_ids": ["ev1"],
                                  "times": [{"dimension": "chapter", "start": "1.2.1"}]}])
    two = save(store, "乙", relations=[{"kind": "temporal", "predicate": "after", "target_id": one["id"],
        "target_version": 1, "source_event_ids": ["ev1"]}])
    result = await KnowledgeRetrieval(store, Orthogonal(), "model").recall("不存在的词", purpose="memory_match",
        temporal={"dimension": "chapter", "start": "1.2.1"})
    assert result["routes"]["semantic"]["count"] == result["routes"]["keyword"]["count"] == 0
    assert {r["record"]["id"] for r in result["items"]} == {one["id"], two["id"]}
    assert all("temporal" in r["ranks"] for r in result["items"])


@pytest.mark.asyncio
async def test_context_callback_only_after_model_response_and_in_finalize():
    from novelagent.core.llm_turn import query
    callbacks = []
    class LLM:
        def __init__(self, error=False):
            self.error = error
        async def chat(self, **kwargs):
            yield SimpleNamespace(type="error", error="failed") if self.error else SimpleNamespace(type="text_delta", content="完成")
    async def callback(messages):
        callbacks.append(messages)
    async def execute(*args):
        raise AssertionError("No tool should run")
    async def run(error, turns):
        return [e async for e in query(LLM(error), position="sub_agent", messages=[{"role": "user", "content": "x"}],
            system_prompt="", tools=None, max_turns=turns, tag="t", execute_tool=execute, on_model_response=callback)]
    await run(True, 1)
    assert callbacks == []
    await run(False, 0)
    assert len(callbacks) == 1


def test_api_lists_new_and_legacy_separately_and_reads_versions(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from novelagent.server.routes.traces import router
    project = tmp_path/"p"
    store = KnowledgeStore(project)
    one = save(store)
    store.save_memory(one["payload"], evidence(), [1, 0, 0], "model", one["id"], 1)
    app = FastAPI()
    app.state.agent_loop = SimpleNamespace(working_dir=str(tmp_path))
    app.include_router(router)
    with TestClient(app) as client:
        rows = client.get("/api/projects/p/memories").json()
        assert rows[0]["schema"] == "knowledge_v2" and rows[0]["version"] == 2
        assert client.get(f"/api/projects/p/knowledge/{one['id']}?version=1").json()["version"] == 1
        assert client.post(f"/api/projects/p/memory/{one['id']}/downgrade").status_code == 409


def test_internal_causality_resolves_new_fact_ids_and_entity_identity(tmp_path):
    store = KnowledgeStore(tmp_path)
    statements = [{"statement_id": f"new:{i}", "classifications": [{"domain": "content", "categories": ["setting"]}], "claim": text, "source_event_ids": ["ev1"],
              "entities": [{"name": "林舟", "kind": "person"}]} for i, text in [(1, "家人遭威胁"), (2, "主动调查")]]
    record = save(store, statements=statements, relations=[{"kind": "causal", "predicate": "causes", "target_version": 1,
        "source_statement_id": "new:1", "target_statement_id": "new:2", "domains": ["content"], "source_event_ids": ["ev1"]}])
    relation = record["payload"]["relations"][0]
    assert relation["target_id"] == record["id"]
    assert relation["source_statement_id"].startswith("statement_") and relation["target_statement_id"].startswith("statement_")
    assert len({f["entities"][0]["entity_id"] for f in record["payload"]["statements"]}) == 1


def test_explicit_validity_is_not_inferred_from_source_event_time(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store)
    expired = publish(store, memory, times=[{"dimension": "valid", "start": "2000-01-01", "end": "2001-01-01"}])
    assert not KnowledgeRetrieval.applicable(expired, "main")
    ordinary = publish(store, memory)
    assert KnowledgeRetrieval.applicable(ordinary, "main")
    local = publish(store, memory, times=[{"dimension": "chapter", "start": "1.2.0", "end": "1.2.999999"}])
    assert KnowledgeRetrieval.applicable(local, "main", ["chapters/content_1.2.4.md"])
    assert not KnowledgeRetrieval.applicable(local, "main", ["chapters/content_1.12.4.md"])


def test_fragment_verification_reads_saved_versions_not_latest_file(tmp_path):
    from novelagent.history import DocumentHistoryStore
    history = DocumentHistoryStore(tmp_path/"p")
    path = tmp_path/"p"/"chapters"/"content_1.1.1.md"
    history.record_revision(path, "rev1", None, "旧的相关句。", "旧的相关句。")
    history.record_revision(path, "rev2", "rev1", "改后的相关句。", "改后的相关句。")
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "model")
    data = package(artifact_paths=["chapters/content_1.1.1.md"], fragments=[{
        "path": "chapters/content_1.1.1.md", "revision_id": "rev2", "previous_revision_id": "rev1",
        "before": "旧的相关句", "after": "改后的相关句"}])
    pipeline._validate_fragments("p", data, set(data["artifact_paths"]))
    data["fragments"][0]["after"] = "不在存档中的文字"
    with pytest.raises(ValueError, match="cannot be verified"):
        pipeline._validate_fragments("p", data, set(data["artifact_paths"]))


def test_migration_audit_does_not_write_or_rename_legacy_data(tmp_path):
    from scripts.audit_knowledge_migration import audit
    path = tmp_path/".memory"/"project"/"old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: old\nhistory_ids: [missing]\n---\n旧正文", encoding="utf-8")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = audit(tmp_path)
    assert result["mode"] == "read_only" and result["candidates"][0]["missing_history_ids"] == ["missing"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_subagent_waits_for_knowledge_and_counts_the_actual_context(tmp_path):
    from novelagent.core.subagent import SubAgentRunner
    from novelagent.core.session import Session
    from novelagent.tools.registry import ToolRegistry
    from novelagent.tools.search_memory import ReadHistoryTool
    class LLM:
        def __init__(self):
            self.messages = None
        async def chat(self, **kwargs):
            self.messages = kwargs["messages"]
            yield SimpleNamespace(type="text_delta", content="完成")
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), "model")
    store = pipeline.store("p")
    memory = save(store)
    insight = publish(store, memory, roles=["chapter_writer"])
    runtime = KnowledgeRuntime(pipeline)
    registry = ToolRegistry()
    registry.register(SearchMemoryTool(pipeline))
    registry.register(ReadHistoryTool(pipeline))
    model = LLM()
    runner = SubAgentRunner(model, registry, None, None, str(tmp_path), knowledge_runtime=runtime)
    runner.presets_tool.get_preset = lambda name: {"system_prompt": "写作", "tools": [], "max_turns": 1,
                                                   "inherit_history": False}
    chunks = [c async for c in runner.spawn_and_run("chapter_writer", "节奏承接", Session("s", "p"))]
    assert chunks[-1].type == "subagent_done"
    assert "Memory 事实包标题窗口" in model.messages[0]["content"]
    assert any(insight["id"] in str(m["content"]) for m in model.messages)
    assert store.view(store.get(insight["id"]))["support_count"] == 1
