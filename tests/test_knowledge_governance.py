import json
from types import SimpleNamespace

import numpy as np
import pytest

from novelagent.memory.models import MemoryPackage, Insight
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.retrieval import KnowledgeRetrieval


class Embedding:
    async def encode(self, texts):
        return np.asarray([[1., 0., 0.] for _ in texts], dtype=np.float32)


def save(store, **overrides):
    raw = {'title': '节奏', 'direction': '节奏', 'background': '动作场景', 'requirement': '保持节奏',
           'statements': [{'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'claim': '保持节奏', 'source_event_ids': ['ev']}]}
    raw.update(overrides)
    payload = MemoryPackage.model_validate(raw).model_dump()
    return store.save_memory(payload, [{'event_id': 'ev', 'history_id': 'h', 'kind': 'user_input', 'content': '保持节奏'}], [1, 0, 0], 'model')


def entry(memory, **overrides):
    payload = Insight.model_validate({'title': '节奏方法', 'claim': '保持节奏', 'kind': 'method', 'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'purpose': '自然承接',
        'sources': [{'memory_id': memory['id'], 'version': memory['version'],
                     'statement_ids': [memory['payload']['statements'][0]['statement_id']]}]}).model_dump()
    return {'payload': payload, 'evidence': memory['evidence'], 'vector': [1, 0, 0], 'model_id': 'model', **overrides}


def publish(store, memory):
    return store.publish_insights([entry(memory)], {memory['id']: memory['version']})[0]


@pytest.mark.asyncio
async def test_hnsw_persists_and_reuses_verified_snapshot_after_restart(tmp_path):
    store = KnowledgeStore(tmp_path)
    save(store)
    first = await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    second = await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    assert first['index_cache']['source'] == 'rebuilt'
    assert second['index_cache']['source'] == 'persistent'
    assert first['items'][0]['record']['id'] == second['items'][0]['record']['id']


@pytest.mark.asyncio
async def test_torn_hnsw_sidecar_is_rebuilt_not_trusted(tmp_path):
    store = KnowledgeStore(tmp_path)
    save(store)
    await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    (tmp_path / '.knowledge/indexes/memory_match.usearch').write_bytes(b'corrupt cache')
    result = await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    assert result['status'] == 'ok'
    assert result['index_cache'] == {'source': 'rebuilt', 'reason': 'checksum_mismatch'}


@pytest.mark.asyncio
async def test_hnsw_source_version_change_invalidates_persisted_cache(tmp_path):
    store = KnowledgeStore(tmp_path)
    one = save(store)
    await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    store.save_memory(one['payload'], one['evidence'], [1, 0, 0], 'model', one['id'], 1)
    result = await KnowledgeRetrieval(store, Embedding(), 'model').recall('节奏', purpose='memory_match')
    assert result['items'][0]['record']['version'] == 2 and result['index_cache']['reason'] == 'stale'


def test_atomic_merge_keeps_old_id_and_exact_versions_readable(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store)
    old1, old2 = publish(store, memory), publish(store, memory)
    merged = store.publish_insights([entry(memory, replaces=[old1['id'], old2['id']], replacement_reason='同方向综合')],
        {memory['id']: 1}, [old1['id'], old2['id']], withdrawal_versions={old1['id']: 1, old2['id']: 1})[0]
    assert store.get(old1['id'])['status'] == 'withdrawn'
    assert store.view(store.get(old1['id'], 1))['replacements'] == [{'new_id': merged['id'], 'new_version': 1, 'reason': '同方向综合'}]
    assert [r['id'] for r in store.list(('insight',))] == [merged['id']]
    assert any(e['properties']['predicate'] == 'replaces' for e in store.graph()['edges'])


def test_stale_withdrawal_rolls_back_entire_publication(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store)
    old = publish(store, memory)
    with pytest.raises(ValueError, match='changed during'):
        store.publish_insights([entry(memory)], {memory['id']: 1}, [old['id']], withdrawal_versions={old['id']: 2})
    assert len(store.list(('insight',))) == 1 and store.get(old['id'])['status'] == 'active'


def test_invalid_replacement_mapping_rolls_back_new_record(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store)
    with pytest.raises(ValueError, match='withdrawn'):
        store.publish_insights([entry(memory, replaces=['missing'])], {memory['id']: 1})
    assert store.list(('insight',)) == []


def test_demand_merge_deduplicates_maturity_across_attempts(tmp_path):
    store = KnowledgeStore(tmp_path)
    insight = publish(store, save(store))
    store.mark_used(insight['id'], 1, 'old', '', 'main', threshold=0)
    store.mark_used(insight['id'], 1, 'new', '', 'reviewer', threshold=2)
    assert store.get(insight['id'])['layer'] == 'pattern'
    store.merge_task_uses('root', ['old', 'new'], threshold=2)
    assert store.view(store.get(insight['id']))['support_count'] == 1
    assert store.get(insight['id'])['layer'] == 'insight'


def person(background, new=False):
    return {'background': background, 'statements': [{'classifications': [{'domain': 'content', 'categories': ['setting']}], 'claim': background, 'source_event_ids': ['ev'],
                                                'entities': [{'name': '林舟', 'kind': 'person', 'new_identity': new}]}]}


def test_same_name_can_have_distinct_entity_ids(tmp_path):
    store = KnowledgeStore(tmp_path)
    one = save(store, **person('林舟是主角'))
    two = save(store, **person('另一个时代的同名人物', new=True))
    assert one['payload']['statements'][0]['entities'][0]['entity_id'] != two['payload']['statements'][0]['entities'][0]['entity_id']
    assert len(store.entity_candidates(['林舟'], 'person')) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['existing', 'new', 'unresolved'])
async def test_entity_resolution_uses_evidence_not_name_equality(tmp_path, choice):
    pipeline = KnowledgePipeline(None, tmp_path, Embedding(), 'model')
    memory = save(pipeline.store('p'), **person('主角林舟'))
    eid = memory['payload']['statements'][0]['entities'][0]['entity_id']
    class LLM:
        async def chat(self, **kwargs):
            assert kwargs['tag'].endswith('entity-resolution')
            yield SimpleNamespace(type='text_delta', content=json.dumps({'entities': [{'index': 0,
                'entity_id': eid if choice == 'existing' else '', 'new_identity': choice == 'new',
                'ambiguous': choice == 'unresolved', 'reason': '根据事实背景'}]}))
    pipeline.llm = LLM()
    package = MemoryPackage.model_validate({'title': '人物', 'direction': '人物', 'requirement': '记录人物', **person('林舟身份待核')}).model_dump()
    await pipeline._resolve_entities('p', package, memory['evidence'])
    saved = pipeline.store('p').save_memory(package, memory['evidence'], [1, 0, 0], 'model')
    entity = saved['payload']['statements'][0]['entities'][0]
    if choice == 'existing':
        assert entity['entity_id'] == eid
    elif choice == 'new':
        assert entity['entity_id'] != eid
    else:
        assert entity['entity_id'] == '' and entity['ambiguous']
        assert not any(l['id'] == saved['id'] for l in pipeline.store('p').graph()['links'])


@pytest.mark.asyncio
async def test_live_recall_loads_bounded_frontier_without_full_graph_snapshot(tmp_path):
    store = KnowledgeStore(tmp_path)
    target = save(store, **person('主角林舟'))
    for index in range(25):
        save(store, title=f'其他方向 {index}')
    store.graph = lambda: (_ for _ in ()).throw(AssertionError('full graph must not load'))
    result = await KnowledgeRetrieval(store, Embedding(), 'model', graph_nodes=12).recall('林舟', purpose='memory_match')
    assert result['status'] == 'ok' and result['graph_loaded_nodes'] <= 12
    assert any(r['record']['id'] == target['id'] and 'relation' in r['ranks'] for r in result['items'])


def test_graph_frontier_keeps_pinned_old_source_version_visible(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = save(store, **person('主角林舟'))
    insight = publish(store, memory)
    store.save_memory(memory['payload'], memory['evidence'], [1, 0, 0], 'model', memory['id'], 1)
    graph = store.graph_neighborhood([(insight['id'], 1)], '林舟', nodes=10)
    assert any(l['id'] == memory['id'] and l['version'] == 1 for l in graph['links'])
    assert any(e['target_id'] == memory['id'] and e['target_version'] == 1 for e in graph['edges'])


def test_multi_file_facts_get_their_own_chapter_anchor_not_the_first_file(tmp_path):
    paths = ['chapters/content_1.1.1.md', 'chapters/content_1.2.1.md']
    raw = {'title': '两个修改方向', 'direction': '节奏', 'background': '跨文件调整', 'requirement': '保持承接',
           'artifact_paths': paths, 'statements': [
               {'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'claim': '第二节的修改', 'source_event_ids': ['ev2']},
               {'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'claim': '无法定位具体节的意见', 'source_event_ids': ['ev1']}]}
    evidence = [{'event_id': 'ev1', 'kind': 'user_input', 'content': '保持承接'},
                {'event_id': 'ev2', 'kind': 'polisher_thinking', 'content': '第二节修改', 'scope': {'path': paths[1]}}]
    result = KnowledgePipeline._validate_package(raw, evidence)
    assert result['statements'][0]['times'][0]['start'] == '1.2.1'
    assert result['statements'][1]['times'] == []
