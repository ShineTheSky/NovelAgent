"""Memory creation/graph/learning contracts; temporary stores and no network."""

from copy import deepcopy
import json

import numpy as np
import pytest
from tests.conftest import quoted_memory_response

from novelagent.llm.client import LLMResponse
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.models import MemoryPackage, Insight, GraphRelation
from novelagent.memory.retrieval import KnowledgeRetrieval


def evidence(event="e", stamp=""):
    return [{"event_id": event, "history_id": "h_" + event, "kind": "user_input",
             "content": "修改承接", "occurred_at": stamp}]


def package(event="e", title="承接", times=()):
    return MemoryPackage.model_validate({"title": title, "direction": "承接", "background": "原段落",
        "requirement": "修改承接", "statements": [{"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": title,
            "source_event_ids": [event], "times": list(times)}]}).model_dump()


def save(store, event="e", vector=(1, 0), times=(), stamp="", graph=False):
    return store.save_memories([{"payload": package(event, times=times), "evidence": evidence(event, stamp),
        "vector": vector, "model_id": "test"}], build_graph=graph)[0]


class Embedding:
    async def encode(self, texts):
        return [[1., 0.] for _ in texts]


class Model:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        response = self.handler(kwargs)
        if isinstance(response, Exception):
            raise response
        yield LLMResponse(type="text_delta", content=json.dumps(quoted_memory_response(response, kwargs['messages']), ensure_ascii=False))


def data(call, name, next_name=None):
    value = call["messages"][0]["content"].split("\n" + name + ":", 1)[1]
    return json.loads(value.split("\n" + next_name + ":", 1)[0] if next_name else value)


def edge(a, b, kind="semantic", predicate="related"):
    return GraphRelation.model_validate({"owner_id": a["id"], "owner_version": a["version"],
        "kind": kind, "predicate": predicate, "target_id": b["id"], "target_version": b["version"],
        "source_statement_id": a["payload"]["statements"][0]["statement_id"],
        "target_statement_id": b["payload"]["statements"][0]["statement_id"],
        "source_event_ids": [a["payload"]["statements"][0]["source_event_ids"][0]]}).model_dump()


@pytest.mark.asyncio
async def test_pure_vector_ranking_excludes_self_and_reuses_query_vector(tmp_path):
    store = KnowledgeStore(tmp_path)
    same = save(store, "a")
    nearest = save(store, "b", (.99, np.sqrt(1-.99**2)))
    farther = save(store, "c", (.8, .6))
    class NoEncode:
        async def encode(self, texts):
            raise AssertionError("query vector must be reused")
    retrieval = KnowledgeRetrieval(store, NoEncode(), "test")
    items = await retrieval.vector_candidates([1, 0], limit=2, exclude=[same["id"]])
    assert [i["record"]["id"] for i in items] == [nearest["id"], farther["id"]]
    assert items[0]["similarity"] == pytest.approx(.99)
    assert "score" not in items[0] and retrieval._index is not None
    with store.connect() as db:
        audit = json.loads(db.execute("SELECT payload FROM audits").fetchone()[0])
    assert audit["purpose"] == "memory_vector" and audit["elapsed_ms"] >= 0
    with pytest.raises(ValueError, match="model changed"):
        await KnowledgeRetrieval(store, NoEncode(), "other").vector_candidates([1, 0])


@pytest.mark.asyncio
@pytest.mark.parametrize("similarity,action", [(.8, "new"), (.97, "new"), (.971, "new"), (.99, "update"), (.99, "defer")])
async def test_strict_similarity_gate_does_not_force_same_direction_merge(tmp_path, monkeypatch, similarity, action):
    def handler(call):
        if call["tag"].endswith("retain"):
            return {"memories": [package("next")]}
        assert call["tag"].endswith("match")
        merged = deepcopy(prior["payload"])
        merged["statements"].append(package("next")["statements"][0])
        return {"action": action, "memory_id": prior["id"], "expected_version": 1,
                "memory": merged if action == "update" else None, "reason": "独立经历或补充"}
    model = Model(handler)
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test")
    prior = save(pipeline.store("p"))
    class Retrieval:
        async def vector_candidates(self, *args, **kwargs):
            return [{"record": prior, "similarity": similarity}]
    async def no_learning(*args, **kwargs):
        pass
    monkeypatch.setattr(pipeline, "retrieval", lambda p: Retrieval())
    monkeypatch.setattr(pipeline, "consolidate_pending", no_learning)
    unit = {"history_id": "next", "kind": "conversation", "status": "completed", "events": evidence("next")}
    result = await pipeline.process("p", unit)
    assert len(model.calls) == (2 if similarity > .97 else 1)
    store = pipeline.store("p")
    if action == "defer":
        assert not result and len(store.list()) == 1
    elif action == "update":
        assert len(store.list()) == 1 and store.get(prior["id"])["version"] == 2
        assert store.get(prior["id"], 1)["payload"] == prior["payload"]
    else:
        assert len(store.list()) == 2 and store.get(prior["id"])["version"] == 1


def test_graph_edges_are_version_pinned_undirected_deduplicated_and_causal_directed(tmp_path):
    store = KnowledgeStore(tmp_path)
    a, b = save(store, "a", graph=True), save(store, "b", graph=True)
    related, backward = edge(a, b), edge(b, a)
    cause = edge(a, b, "causal", "causes")
    assert related["direction"] == "undirected" and cause["direction"] == "directed"
    store.finish_graph_job(a["id"], 1, [related, backward, cause], {a["id"]: 1, b["id"]: 1})
    assert store.get(a["id"])["payload"] == a["payload"]
    graph = store.graph()
    assert len(graph["edges"]) == 2
    assert any(
        e["owner_id"] == a["id"] and e["target_id"] == b["id"] and e["kind"] == "causal" for e in graph["edges"])
    adj, _, _ = KnowledgeRetrieval._statement_graph(graph)
    destination = (b["id"], 1, b["payload"]["statements"][0]["statement_id"])
    assert any(label.startswith("semantic:related") for _, label in adj[destination])
    assert any(label.startswith("reverse:causal:causes") for _, label in adj[destination])
    with pytest.raises(ValueError, match="must be directed"):
        GraphRelation.model_validate({**cause, "direction": "undirected"})
    assert {r["id"] for r in store.related_memories(a["id"], 1)} == {b["id"]}


@pytest.mark.parametrize("dimension", ["observed", "occurred"])
def test_time_edges_separate_same_section_and_proximity(tmp_path, dimension):
    store = KnowledgeStore(tmp_path)
    def anchors(position, moment):
        return [{"dimension": "chapter", "start": position}, {"dimension": dimension, "start": moment}]
    a = save(store, "a", times=anchors("1.1.2", "2020-01-01T00:00:00Z"))
    b = save(store, "b", times=anchors("1.1.2", "2020-01-02T00:00:00Z"))
    c = save(store, "c", times=anchors("1.1.3", "2020-01-01T01:00:00Z"))
    far = save(store, "far", times=anchors("1.1.4", "2025-01-01T00:00:00Z"))
    edges = store.time_relations(a["id"], 1)
    assert {e["target_id"] for e in edges if e["predicate"] == "same_section"} == {b["id"]}
    assert {e["target_id"] for e in edges if e["predicate"] == "near_time"} == {b["id"], c["id"]}
    assert all(e["direction"] == "undirected" for e in edges)
    assert far["id"] not in {e["target_id"] for e in edges}
    assert not any(e["scope"] == "created" for e in edges)


def test_source_time_not_migration_creation_time_and_neighbor_bound(tmp_path):
    store = KnowledgeStore(tmp_path)
    source = save(store, "a", stamp="2000-01-01T00:00:00Z")
    recent = save(store, "recent", stamp="2026-01-01T00:00:00Z")
    missing = save(store, "missing")
    for index in range(12):
        save(store, str(index), stamp="2000-01-01T01:00:00Z")
    edges = store.time_relations(source["id"], 1, neighbors=3)
    assert len(edges) == 3 and all(e["predicate"] == "near_time" for e in edges)
    assert {recent["id"], missing["id"]}.isdisjoint(e["target_id"] for e in edges)


@pytest.mark.asyncio
async def test_batch_graph_one_call_before_learning_and_related_memory_in_input(tmp_path):
    def handler(call):
        if call["tag"].endswith("retain"):
            return {"memories": [package("e", "段落承接"), package("e", "人物心理")]}
        if call["tag"].endswith("memory_graph"):
            memories = data(call, "Memories")
            return {"relations": [edge(memories[0], memories[1])]}
        assert call["tag"].endswith("consolidate")
        memories = data(call, "Memories", "Existing")
        assert len(memories) == 2
        assert all(pipeline.store("p").graph_job(m["id"], m["version"])["status"] == "complete" for m in memories)
        return {"insights": [], "withdrawals": []}
    model = Model(handler)
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test")
    unit = {"history_id": "h", "kind": "conversation", "status": "completed", "events": evidence()}
    assert await pipeline.process("p", unit)
    assert [c["tag"] for c in model.calls] == [":knowledge/retain", ":knowledge/memory_graph", ":knowledge/consolidate"]
    assert len(pipeline.store("p").graph()["edges"]) == 1
    # Same graph/source prompt is cached; all version jobs complete without duplicate model requests.
    assert not pipeline.store("p").jobs()
    await pipeline.process("p", unit)
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_graph_failure_preserves_memory_and_old_insight_then_explicit_retry(tmp_path):
    fail = True
    def handler(call):
        if call["tag"].endswith("retain"):
            return {"memories": [package("new")]}
        if call["tag"].endswith("memory_graph"):
            if fail:
                return RuntimeError("graph unavailable")
            memories = data(call, "Memories")
            return {"relations": [edge(memories[0], memories[1])]}
        return {"insights": [], "withdrawals": []}
    class Cases:
        def __init__(self): self.calls = []
        async def capture(self, **kwargs): self.calls.append(kwargs)
    cases = Cases()
    model = Model(handler)
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test", bad_cases=cases)
    store = pipeline.store("p")
    old = save(store, "old", (.8, .6))
    old_insight = Insight.model_validate({"title": "旧约束", "claim": "保持承接", "kind": "rule", "classifications": [{"domain": "writing", "categories": ["polishing"]}], "purpose": "写作",
        "sources": [{"memory_id": old["id"], "version": 1}]}).model_dump()
    insight = store.publish_insights([{"payload": old_insight, "evidence": old["evidence"], "vector": [1, 0],
                                      "model_id": "test"}], {old["id"]: 1})[0]
    unit = {"history_id": "new", "kind": "conversation", "status": "completed", "events": evidence("new")}
    assert await pipeline.process("p", unit)
    new = next(m for m in store.list(("memory",)) if m["id"] != old["id"])
    assert store.graph_job(new["id"], 1)["status"] == "failed"
    assert cases.calls and len(model.calls) == 2
    assert store.get(insight["id"])["version"] == 1 and store.get(insight["id"])["status"] == "active"
    await pipeline.consolidate_pending("p")
    assert len(model.calls) == 2
    fail = False
    await pipeline.consolidate_pending("p", retry_failed=True)
    assert store.graph_job(new["id"], 1)["status"] == "complete" and not store.jobs()
    assert len(store.list(("memory",))) == 2 and len(store.graph()["edges"]) == 2  # source + related


@pytest.mark.asyncio
async def test_graph_validation_feedback_repairs_endpoints_and_records_bad_case(tmp_path):
    count = 0
    def handler(call):
        nonlocal count
        if call["tag"].endswith("memory_graph"):
            count += 1
            memories = data(call, "Memories")
            relation = edge(memories[0], memories[1])
            if count == 1:
                relation["target_statement_id"] = "missing"
            else:
                details = json.loads(call["messages"][-1]["content"].split("\n校验详情：", 1)[1])
                assert details["location"] == "relations[0]" and details["supplied_fact_id"] == "missing"
            return {"relations": [relation]}
        return {"insights": [], "withdrawals": []}
    class Cases:
        def __init__(self): self.calls = []
        async def capture(self, **kwargs): self.calls.append(kwargs)
    cases = Cases()
    pipeline = KnowledgePipeline(Model(handler), tmp_path, Embedding(), "test", bad_cases=cases,
                                 config={"analysis_max_turns": 2})
    store = pipeline.store("p")
    a, b = save(store, "a", graph=True), save(store, "b", graph=True)
    await pipeline._build_memory_graph("p", store.graph_job(a["id"], 1))
    assert count == 2 and len(cases.calls) == 1
    assert cases.calls[0]["params"]["error_kind"] == "validation"
    assert store.graph_job(a["id"], 1)["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "withdraw", "unchanged"])
async def test_independent_new_memory_coordinates_existing_insight_not_memory(tmp_path, action):
    def handler(call):
        if call["tag"].endswith("retain"):
            return {"memories": [package("new", "恢复舒缓承接")]}
        if call["tag"].endswith("memory_graph"):
            memories = data(call, "Memories")
            return {"relations": [edge(memories[0], memories[1])]}
        assert call["tag"].endswith("consolidate")
        memories = data(call, "Memories", "Existing")
        existing = data(call, "Existing")
        assert {m["id"] for m in memories} == {old["id"], new["id"]}
        assert insight["id"] in {i["id"] for i in existing}
        if action == "update":
            payload = deepcopy(insight["payload"])
            payload.update(claim="先保留动作承接，再按场景调整节奏", sources=[
                {"memory_id": new["id"], "version": 1, "statement_ids": [new["payload"]["statements"][0]["statement_id"]]},
                {"memory_id": old["id"], "version": 1, "role": "context"}])
            return {"insights": [{"id": insight["id"], "expected_version": 1, "payload": payload}], "withdrawals": []}
        return {"insights": [], "withdrawals": [insight["id"]] if action == "withdraw" else []}
    model = Model(handler)
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), "test")
    store = pipeline.store("p")
    old = save(store, "old", (.8, .6))
    payload = Insight.model_validate({"title": "节奏承接", "claim": "快速承接", "kind": "method", "classifications": [{"domain": "writing", "categories": ["polishing"]}], "purpose": "指导写作",
        "sources": [{"memory_id": old["id"], "version": 1}]}).model_dump()
    insight = store.publish_insights([{"payload": payload, "evidence": old["evidence"], "vector": [1, 0],
                                      "model_id": "test"}], {old["id"]: 1})[0]
    store.mark_used(insight["id"], 1, "task", "", "chapter_writer")
    with store.connect() as db:
        initial_weight = db.execute("SELECT base_weight FROM maturity WHERE id=?", (insight["id"],)).fetchone()[0]
    async def no_learning(*args, **kwargs): pass
    # Save the fact package first, then exercise the real graph + reconciliation stages.
    original = pipeline.consolidate_pending
    pipeline.consolidate_pending = no_learning
    await pipeline.process("p", {"history_id": "new", "kind": "conversation", "status": "completed", "events": evidence("new")})
    new = next(m for m in store.list(("memory",)) if m["id"] != old["id"])
    pipeline.consolidate_pending = original
    await pipeline.consolidate_pending("p")
    result = store.get(insight["id"])
    assert not store.jobs() and store.get(old["id"])["version"] == 1
    assert len(store.list(("memory",))) == 2
    if action == "update":
        assert result["version"] == 2 and result["payload"]["sources"][0]["memory_id"] == new["id"]
    elif action == "withdraw":
        assert result["status"] == "withdrawn" and store.get(insight["id"], 1) is not None
    else:
        assert result["version"] == 1 and result["status"] == "active"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM uses WHERE id=?", (insight["id"],)).fetchone()[0] == 1
        assert db.execute("SELECT base_weight FROM maturity WHERE id=?", (insight["id"],)).fetchone()[0] == initial_weight


def test_graph_transaction_rolls_back_and_never_rewrites_fact_payload(tmp_path):
    store = KnowledgeStore(tmp_path)
    a, b = save(store, "a", graph=True), save(store, "b")
    valid, invalid = edge(a, b), edge(a, b, "causal", "causes")
    invalid["target_statement_id"] = "missing"
    with pytest.raises(ValueError, match="target fact"):
        store.finish_graph_job(a["id"], 1, [valid, invalid], {a["id"]: 1, b["id"]: 1})
    assert not store.graph()["edges"] and store.graph_job(a["id"], 1)["status"] == "pending"
    assert store.get(a["id"])["payload"] == a["payload"]
