"""Three-domain contract and immutable legacy reads; no external model calls."""

import json
import sqlite3
from copy import deepcopy

import numpy as np
import pytest
from pydantic import ValidationError

from novelagent.memory.models import (
    CATEGORIES, Classification, Insight, MemoryPackage, MemoryStatement, SourceMemory,
    retrieval_text, require_classifications, exclude_dispatch_entities,
)
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.offline_migration import preview_knowledge_classifications
from novelagent.memory.retrieval import KnowledgeRetrieval
from novelagent.memory.runtime import KnowledgeRuntime
from types import SimpleNamespace


def statement(domain="writing", categories=None, **extra):
    return {"claim": "补足动作承接", "source_event_ids": ["e"],
            "classifications": [{"domain": domain, "categories": categories or ["polishing"]}], **extra}


def memory(store, statements=None):
    return store.save_memory({"title": "动作承接", "direction": "动作承接", "background": "短句切断动作",
        "requirement": "调整衔接", "artifact_paths": ["chapters/content_1.1.1.md", "outlines/outline_1.1.0.md"],
        "statements": statements or [statement()]},
        [{"event_id": "e", "history_id": "h", "kind": "user_input", "content": "调整衔接"}], [1., 0.], "fixture")


def insight(memory, **extra):
    return {"title": "承接方法", "claim": "用动作承接短句", "kind": "method", "purpose": "指导润色",
        "classifications": [{"domain": "writing", "categories": ["polishing", "reviewing"]}],
        "roles": ["chapter_polisher", "reviewer"], "authority": "agent",
        "sources": [{"memory_id": memory["id"], "version": memory["version"],
                     "statement_ids": [memory["payload"]["statements"][0]["statement_id"]]}], **extra}


@pytest.mark.parametrize("domain,category", [(d, c) for d, categories in CATEGORIES.items() for c in sorted(categories)])
def test_all_first_level_categories(domain, category):
    assert Classification(domain=domain, categories=[category, category]).categories == [category]


@pytest.mark.parametrize("domain,category", [("content", "polishing"), ("writing", "character"), ("agent_work", "setting")])
def test_reject_wrong_domain_category(domain, category):
    with pytest.raises(ValidationError):
        Classification(domain=domain, categories=[category])


def test_memory_aggregates_active_statements_without_splitting_direction(tmp_path):
    record = memory(KnowledgeStore(tmp_path), [
        statement("content", ["character", "organization"]),
        statement(categories=["outline", "polishing"]),
        statement("agent_work", ["task_dispatch"], status="withdrawn"),
    ])
    assert record["payload"]["classifications"] == [
        {"domain": "content", "categories": ["character", "organization"]},
        {"domain": "writing", "categories": ["outline", "polishing"]},
    ]
    assert len(KnowledgeStore(tmp_path).list(("memory",))) == 1


def test_dispatch_entities_are_removed_per_statement_without_losing_evidence(tmp_path):
    store = KnowledgeStore(tmp_path)
    dispatch = statement("agent_work", ["task_dispatch"], claim="主 Agent 要求限定修改范围并传递验收条件",
        entities=[{"name": "主 Agent", "kind": "agent"}, {"name": "user", "kind": "user"},
                  {"name": "chapters/content_1.1.1.md", "kind": "artifact"}],
        modality="planned", attributes={"artifact_paths": ["chapters/content_1.1.1.md"]},
        times=[{"dimension": "chapter", "start": "1.1.1"}])
    business = statement("content", ["character"], entities=[{"name": "林舟", "kind": "person"}])
    original = deepcopy(dispatch)
    saved = memory(store, [dispatch, business])
    actual = saved["payload"]["statements"][0]
    assert actual["entities"] == []
    for key in ("claim", "source_event_ids", "modality", "attributes"):
        assert actual[key] == original[key]
    assert actual["times"][0]["start"] == "1.1.1"
    assert dispatch == original  # Neither caller data nor immutable old records are normalized in place.
    assert saved["evidence"][0]["history_id"] == "h"
    graph = store.graph()
    assert {entity["name"] for entity in graph["entities"]} == {"林舟"}
    assert {link["statement_id"] for link in graph["links"]} == {saved["payload"]["statements"][1]["statement_id"]}
    assert actual["statement_id"] in {row["statement_id"] for row in graph["times"]}
    assert actual["claim"] in retrieval_text("memory", saved["payload"])

    updated = deepcopy(saved["payload"])
    updated["statements"][0]["entities"] = original["entities"]
    latest = store.save_memory(updated, saved["evidence"], [1., 0.], "fixture", saved["id"], 1)
    assert latest["version"] == 2 and latest["payload"]["statements"][0]["entities"] == []
    assert store.get(saved["id"], 1)["payload"] == saved["payload"]


@pytest.mark.parametrize("domain,categories", [
    ("content", ["setting"]), ("writing", ["polishing"]),
    *(("agent_work", [category]) for category in sorted(CATEGORIES["agent_work"] - {"task_dispatch"})),
])
def test_dispatch_entity_exception_does_not_apply_to_other_classifications(domain, categories):
    package = MemoryPackage.model_validate({"title": "事实", "direction": "方向", "background": "背景",
        "requirement": "需求", "statements": [statement(domain, categories,
            entities=[{"name": "对象", "kind": "concept"}])]}).model_dump()
    original = deepcopy(package)
    assert exclude_dispatch_entities(package) == original


@pytest.mark.parametrize("kind,predicate", [("causal", "causes"), ("semantic", "related"), ("temporal", "same_section")])
def test_dispatch_keeps_non_entity_graph_edges(tmp_path, kind, predicate):
    store = KnowledgeStore(tmp_path)
    saved = store.save_memory({"title": "传达边界", "direction": "修改范围", "background": "关联章节需要同步",
        "requirement": "同步关联内容", "interpretation": "主 Agent 收窄修改范围",
        "statements": [statement("agent_work", ["task_dispatch"], entities=[{"name": "主 Agent", "kind": "agent"}]),
                       statement("writing", ["polishing"], claim="关联章节未同步")],
        "relations": [{"kind": kind, "predicate": predicate, "target_version": 1,
                       "source_statement_id": "new:1", "target_statement_id": "new:2", "source_event_ids": ["e"]}]},
        [{"event_id": "e", "history_id": "h", "kind": "main_delegation", "content": "修改范围"}], [1., 0.], "fixture")
    graph = store.graph()
    assert graph["links"] == []
    assert len(graph["edges"]) == 1 and graph["edges"][0]["kind"] == kind
    assert graph["edges"][0]["properties"]["source_event_ids"] == ["e"]
    assert saved["payload"]["interpretation"] == "主 Agent 收窄修改范围"


def test_old_dispatch_payload_is_not_changed_by_read(tmp_path):
    store = KnowledgeStore(tmp_path)
    saved = memory(store, [statement("agent_work", ["task_dispatch"])])
    legacy = deepcopy(saved["payload"])
    legacy["statements"][0]["entities"] = [{"name": "主 Agent", "kind": "agent"}]
    raw = json.dumps(legacy, ensure_ascii=False)
    with store.connect() as db:
        db.execute("UPDATE versions SET payload=? WHERE id=?", (raw, saved["id"]))
    before = store.db_path.read_bytes()
    assert store.get(saved["id"])["payload"]["statements"][0]["entities"][0]["name"] == "主 Agent"
    assert store.db_path.read_bytes() == before


def test_insight_classifies_conclusion_independently_of_content_source(tmp_path):
    store = KnowledgeStore(tmp_path)
    source = memory(store, [statement("content", ["character"])])
    learned = Insight.model_validate(insight(source)).model_dump()
    assert learned["classifications"] == [{"domain": "writing", "categories": ["polishing", "reviewing"]}]
    assert learned["authority"] == "agent"
    assert "category" not in learned and "domain" not in learned and "knowledge_domains" not in learned


def test_legacy_names_keep_ids_modalities_and_attributes():
    old = {"fact_id": "fact_old", "domain": "content", "claim": "计划加入青云门", "modality": "planned",
           "source_event_ids": ["e"], "attributes": {"fact_id": "literal business attribute"}}
    adapted = MemoryStatement.model_validate(old).model_dump()
    assert adapted["statement_id"] == "fact_old" and adapted["modality"] == "planned"
    assert adapted["classifications"] == [{"domain": "content", "categories": []}]
    assert adapted["attributes"] == old["attributes"]
    assert SourceMemory(memory_id="m", version=1, fact_ids=["fact_old"]).statement_ids == ["fact_old"]
    assert old["domain"] == "content" and "classifications" not in old


def test_conflicting_aliases_are_not_silently_overwritten():
    with pytest.raises(ValidationError, match="Conflicting legacy"):
        SourceMemory(memory_id="m", version=1, fact_ids=["a"], statement_ids=["b"])


def test_new_extraction_requires_labels_but_legacy_reads_allow_pending():
    pending = MemoryStatement.model_validate({"domain": "writing", "claim": "旧方法", "source_event_ids": ["e"]})
    with pytest.raises(ValueError, match="classifications"):
        require_classifications(pending.model_dump())
    require_classifications(statement())


def test_legacy_database_read_and_preview_preserve_payload_versions_scores(tmp_path):
    store = KnowledgeStore(tmp_path)
    source = memory(store)
    learned = store.publish_insights([{"payload": insight(source), "evidence": source["evidence"],
        "vector": [1., 0.], "model_id": "fixture"}], {source["id"]: 1})[0]
    old = deepcopy(source["payload"])
    old.pop("classifications")
    old["category"], old["domain"] = "agent", "outline"
    old["facts"] = old.pop("statements")
    old["fact_changes"] = old.pop("statement_changes")
    for value in old["facts"]:
        value["fact_id"] = value.pop("statement_id")
        value.pop("classifications")
        value["domain"] = "writing"
    raw = json.dumps(old, ensure_ascii=False)
    with store.connect() as db:
        db.execute("UPDATE versions SET payload=? WHERE id=?", (raw, source["id"]))
    before = store.db_path.read_bytes()
    report = preview_knowledge_classifications(tmp_path)
    assert store.db_path.read_bytes() == before
    assert report["versions_checked"] == 2 and not report["errors"]
    assert report["pending_classification"] == 1
    read = store.get(source["id"], 1)
    assert read["status"] == "active" and read["version"] == 1
    assert read["payload"]["statements"][0]["statement_id"] == source["payload"]["statements"][0]["statement_id"]
    assert read["legacy_classification"] == {"category": "agent", "domain": "outline"}
    assert store.view(read)["schema"] == "knowledge_v2"
    assert store.view(store.get(learned["id"]))["weight"] == 65
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT payload FROM versions WHERE id=?", (source["id"],)).fetchone()[0] == raw
        assert db.execute("SELECT count(*) FROM uses").fetchone()[0] == 0


def test_classification_does_not_change_embedding_text_or_gate_valid_roles(tmp_path):
    store = KnowledgeStore(tmp_path)
    source = memory(store)
    payload = insight(source)
    changed = {**payload, "classifications": [{"domain": "agent_work", "categories": ["result_verification"]}]}
    assert retrieval_text("insight", payload) == retrieval_text("insight", changed)
    legacy = {"payload": {**payload, "category": "agent"}}
    assert KnowledgeRetrieval.applicable(legacy, "chapter_polisher")
    assert not KnowledgeRetrieval.applicable(legacy, "chapter_writer")
    unconfirmed = {"payload": {**payload, "authority": "reviewer", "human_confirmed": False}}
    assert not KnowledgeRetrieval.applicable(unconfirmed, "chapter_polisher")
    assert KnowledgeRetrieval.applicable(unconfirmed, "reviewer")


@pytest.mark.asyncio
async def test_legacy_agent_method_reaches_context_then_pattern(tmp_path):
    store = KnowledgeStore(tmp_path)
    source = memory(store)
    payload = {**insight(source), "category": "agent", "domain": "writing"}
    learned = store.publish_insights([{"payload": payload, "evidence": source["evidence"],
        "vector": [1., 0.], "model_id": "fixture"}], {source["id"]: 1})[0]
    class Embedding:
        async def encode(self, texts):
            return np.asarray([[1., 0.] for _ in texts])
    retrieval = KnowledgeRetrieval(store, Embedding(), "fixture")
    runtime = KnowledgeRuntime(SimpleNamespace(store=lambda _: store, retrieval=lambda _: retrieval))
    result = await runtime.assemble("p", "用动作承接短句", role="chapter_polisher")
    assert learned["id"] in result["text"] and "写作：润色修改、审阅检查" in result["text"]
    assert store.view(store.get(learned["id"]))["weight"] == 65
    for i in range(9):
        store.mark_used(learned["id"], 1, f"task{i}", "", "chapter_polisher")
    assert store.get(learned["id"])["layer"] == "pattern"
    assert store.get(learned["id"])["payload"]["classifications"] == payload["classifications"]
    result = await runtime.assemble("p", "其他任务", role="chapter_polisher")
    assert any(a["id"] == learned["id"] and a["layer"] == "pattern" for a in result["attachments"])
