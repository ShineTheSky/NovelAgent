"""Fact-level graph recall with temporary SQLite and deterministic embeddings only."""

import json

import numpy as np
import pytest

from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.models import Insight, MemoryPackage
from novelagent.memory.retrieval import KnowledgeRetrieval


class OrthogonalEmbedding:
    async def encode(self, texts):
        return np.asarray([[0., 1., 0.] for _ in texts], dtype=np.float32)


def save(store, facts, relations=()):
    payload = MemoryPackage.model_validate({"title": "独立方向", "direction": "局部经验", "background": "已知背景",
        "requirement": "调整片段", "facts": facts, "relations": list(relations)}).model_dump()
    return store.save_memory(payload, [{"event_id": "e", "history_id": "h", "kind": "user_input", "content": "调整"}],
                             [1., 0., 0.], "test")


def fact(claim, **kwargs):
    return {"domain": "content", "claim": claim, "source_event_ids": ["e"], **kwargs}


def publish(store, memory, fact_indexes=(0,), **kwargs):
    payload = Insight.model_validate({"title": "落实技巧", "claim": "保持结果符合前提", "kind": "method",
        "purpose": "落实", "method": "核对前提与结果", "sources": [{"memory_id": memory["id"], "version": memory["version"],
        "fact_ids": [memory["payload"]["facts"][i]["fact_id"] for i in fact_indexes]}], **kwargs}).model_dump()
    return store.publish_insights([{"payload": payload, "evidence": [], "vector": [1., 0., 0.], "model_id": "test"}],
                                  {memory["id"]: memory["version"]})[0]


def engine(store, **kwargs):
    return KnowledgeRetrieval(store, OrthogonalEmbedding(), "test", **kwargs)


@pytest.mark.asyncio
async def test_entity_causal_path_inside_one_package_reaches_derived_insight(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", fact_id="new:1", entities=[{"name": "独有角色"}]),
                          fact("结果", fact_id="new:2")], [{"kind": "causal", "predicate": "causes", "target_version": 1,
        "source_fact_id": "new:1", "target_fact_id": "new:2", "source_event_ids": ["e"]}])
    insight = publish(store, memory, (1,))
    result = await engine(store).recall("独有角色")
    item = next(item for item in result["items"] if item["record"]["id"] == insight["id"])
    assert set(item["ranks"]) == {"relation"}
    assert any("causal:causes" in step for step in item["paths"])
    assert any(memory["payload"]["facts"][0]["fact_id"] in step for step in item["paths"])
    assert any(memory["payload"]["facts"][1]["fact_id"] in step for step in item["paths"])


@pytest.mark.asyncio
async def test_time_causal_path_projects_memory_fact_to_insight_without_text_hits(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", fact_id="new:1", times=[{"dimension": "chapter", "start": "1.2.3"}]),
                          fact("结果", fact_id="new:2")], [{"kind": "causal", "predicate": "causes", "target_version": 1,
        "source_fact_id": "new:1", "target_fact_id": "new:2", "source_event_ids": ["e"]}])
    insight = publish(store, memory, (1,))
    result = await engine(store).recall("无匹配检索词", temporal={"dimension": "chapter", "start": "1.2.3"})
    item = next(item for item in result["items"] if item["record"]["id"] == insight["id"])
    assert set(item["ranks"]) == {"temporal"}
    assert any("causal:causes" in step for step in item["temporal_path"])
    assert any("semantic:support" in step for step in item["temporal_path"])


@pytest.mark.asyncio
async def test_entity_hit_does_not_jump_to_unrelated_fact_in_same_package(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", entities=[{"name": "独有角色"}]), fact("另一方向")])
    publish(store, memory, (1,))
    result = await engine(store).recall("独有角色")
    assert result["status"] == "empty"


@pytest.mark.asyncio
async def test_temporal_hit_does_not_jump_to_unrelated_fact_in_same_package(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", times=[{"dimension": "chapter", "start": "1.2.3"}]), fact("另一方向")])
    publish(store, memory, (1,))
    result = await engine(store).recall("无匹配检索词", temporal={"dimension": "chapter", "start": "1.2.3"})
    assert result["status"] == "empty"


@pytest.mark.asyncio
async def test_multiple_paths_count_as_one_route_and_do_not_add_fusion_score(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", entities=[{"name": "独有角色", "aliases": ["人物别名"]}]),
                          fact("结果", entities=[{"name": "独有角色"}])])
    insight = publish(store, memory, (0, 1))
    result = await engine(store).recall("独有角色 人物别名")
    assert len(result["items"]) == 1
    assert result["items"][0]["record"]["id"] == insight["id"]
    assert result["items"][0]["ranks"] == {"relation": 1}
    assert result["items"][0]["score"] == pytest.approx(1/61)


@pytest.mark.asyncio
async def test_role_and_artifact_scope_filter_applies_after_graph_projection(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", entities=[{"name": "独有角色"}])])
    insight = publish(store, memory, roles=["reviewer"], authority="reviewer", artifact_paths=["chapters/a.md"])
    assert (await engine(store).recall("独有角色", role="writer", paths=["chapters/a.md"]))["status"] == "empty"
    assert (await engine(store).recall("独有角色", role="reviewer", paths=["chapters/b.md"]))["status"] == "empty"
    result = await engine(store).recall("独有角色", role="reviewer", paths=["chapters/a.md"])
    assert result["items"][0]["record"]["id"] == insight["id"]


@pytest.mark.asyncio
async def test_fact_graph_obeys_hop_budget(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", fact_id="new:1", entities=[{"name": "独有角色"}]), fact("结果", fact_id="new:2")],
        [{"kind": "causal", "predicate": "causes", "target_version": 1, "source_fact_id": "new:1",
          "target_fact_id": "new:2", "source_event_ids": ["e"]}])
    publish(store, memory, (1,))
    assert (await engine(store, graph_depth=1).recall("独有角色"))["status"] == "empty"
    assert (await engine(store, graph_depth=2).recall("独有角色"))["status"] == "ok"


@pytest.mark.asyncio
async def test_withdrawn_only_package_is_not_recalled_by_semantic_or_time(tmp_path):
    store = KnowledgeStore(tmp_path)
    save(store, [fact("独有角色", status="withdrawn", entities=[{"name": "独有角色"}],
                      times=[{"dimension": "chapter", "start": "1.2.3"}])])
    result = await engine(store).recall("独有角色", purpose="memory_match", temporal={"dimension": "chapter", "start": "1.2.3"})
    assert result["status"] == "empty"


def test_inactive_fact_does_not_supply_graph_edge_or_entity_seed():
    graph = {"facts": [{"id": "m", "version": 1, "fact_id": "f1", "status": "withdrawn"}],
        "links": [{"id": "m", "version": 1, "fact_id": "f1", "entity_id": "e"}], "times": [],
        "edges": [{"owner_id": "m", "owner_version": 1, "target_id": "i", "target_version": 1, "kind": "causal",
                   "properties": {"predicate": "causes", "source_fact_id": "f1"}}]}
    adjacency, members, entities = KnowledgeRetrieval._fact_graph(graph)
    assert not adjacency and not members and not entities


@pytest.mark.asyncio
async def test_old_source_fact_remains_recallable_until_insight_atomic_switch(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, [fact("前提", entities=[{"name": "独有角色"}])])
    insight = publish(store, memory)
    payload = memory["payload"]
    payload["facts"][0]["status"] = "withdrawn"
    payload["fact_changes"] = [{"kind": "withdrawn", "previous_fact_ids": [payload["facts"][0]["fact_id"]],
        "reason": "后续否定", "source_event_ids": ["e"]}]
    current = store.save_memory(payload, memory["evidence"], [1., 0., 0.], "test", memory["id"], 1)
    result = await engine(store).recall("独有角色")
    assert result["items"][0]["record"]["id"] == insight["id"]
    store.publish_insights([], {memory["id"]: current["version"]}, [insight["id"]])
    assert (await engine(store).recall("独有角色"))["status"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_causal", [False, True])
async def test_legacy_record_level_derivation_is_projected_to_actual_source_fact(tmp_path, has_causal):
    store = KnowledgeStore(tmp_path)
    relations = [{"kind": "causal", "predicate": "causes", "target_version": 1, "source_fact_id": "new:1",
                  "target_fact_id": "new:2", "source_event_ids": ["e"]}] if has_causal else []
    memory = save(store, [fact("前提", fact_id="new:1", entities=[{"name": "独有角色"}],
                              times=[{"dimension": "chapter", "start": "1.2.3"}]),
                          fact("另一方向", fact_id="new:2")], relations)
    insight = publish(store, memory, (1,))
    legacy_properties = json.dumps({"predicate": "support", "inferred": False})
    # Reproduce v1's immutable record-level edge, not the new fact-level publisher.
    with store.connect() as db:
        db.execute("UPDATE edges SET properties=? WHERE owner_id=? AND owner_version=? AND target_id=? AND kind='semantic'",
                   (legacy_properties, insight["id"], insight["version"], memory["id"]))
    for graph in (store.graph(), store.graph_neighborhood([], "独有角色")):
        support = [edge for edge in graph["edges"] if edge["owner_id"] == insight["id"] and edge["kind"] == "semantic"]
        assert support and {edge["properties"]["target_fact_id"] for edge in support} == {
            memory["payload"]["facts"][1]["fact_id"]}
    entity_result = await engine(store).recall("独有角色")
    time_result = await engine(store).recall("无匹配检索词", temporal={"dimension": "chapter", "start": "1.2.3"})
    if has_causal:
        assert entity_result["items"][0]["record"]["id"] == insight["id"]
        assert time_result["items"][0]["record"]["id"] == insight["id"]
    else:
        assert entity_result["status"] == time_result["status"] == "empty"
    # Reading a legacy projection does not replace or rewrite original evidence.
    with store.connect() as db:
        raw = db.execute("SELECT properties FROM edges WHERE owner_id=? AND target_id=? AND kind='semantic'",
                         (insight["id"], memory["id"])).fetchone()[0]
    assert raw == legacy_properties
