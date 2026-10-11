"""Atomic graph changes and recall-based maturity, with no paid model calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from tests.conftest import quoted_memory_response

from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.models import MemoryPackage, Insight


def evidence():
    return [{'event_id': 'e', 'history_id': 'h', 'kind': 'user_input', 'content': '调整'}]


def package(**kwargs):
    return MemoryPackage.model_validate({'title': '调整', 'direction': '承接', 'background': '原有段落',
        'requirement': '调整', 'statements': [{'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'claim': '保持承接', 'source_event_ids': ['e']}],
        **kwargs}).model_dump()


def entry(payload=None, **kwargs):
    return {'payload': payload or package(), 'evidence': evidence(), 'vector': [1, 0], 'model_id': 'test', **kwargs}


def publish(store, memory, **kwargs):
    payload = Insight.model_validate({'title': '承接方法', 'claim': '保持承接', 'kind': 'method', 'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'purpose': '流畅',
        'sources': [{'memory_id': memory['id'], 'version': memory['version'],
                     'statement_ids': [f['statement_id'] for f in memory['payload']['statements'] if f['status'] == 'active']}]}).model_dump()
    return store.publish_insights([{**entry(payload), **kwargs}], {memory['id']: memory['version']},
                                  kwargs.get('replaces', []))[0]


def test_same_batch_forward_backward_fact_references_and_idempotency(tmp_path):
    store = KnowledgeStore(tmp_path)
    def raw(target):
        return package(relations=[{'kind': 'causal', 'predicate': 'supports', 'target_id': target,
            'target_version': 1, 'source_statement_id': 'new:1', 'target_statement_id': 'new:1', 'source_event_ids': ['e']}])
    entries = [entry(raw('new:2'), operation_id='a'), entry(raw('new:1'), operation_id='b')]
    first, second = store.save_memories(entries)
    assert first['payload']['relations'][0]['target_id'] == second['id']
    assert first['payload']['relations'][0]['target_statement_id'] == second['payload']['statements'][0]['statement_id']
    assert second['payload']['relations'][0]['source_statement_id'] != first['payload']['relations'][0]['source_statement_id']
    repeated = store.save_memories(entries)
    assert [r['id'] for r in repeated] == [first['id'], second['id']]
    assert len(store.list()) == 2 and len(store.graph()['edges']) == 2


@pytest.mark.parametrize('target,statement_id,error', [('new:3', 'new:1', 'Unknown batch'), ('new:2', 'new:9', 'target fact')])
def test_bad_cross_batch_endpoint_rolls_back_every_record(tmp_path, target, statement_id, error):
    store = KnowledgeStore(tmp_path)
    raw = package(relations=[{'kind': 'causal', 'predicate': 'causes', 'target_id': target, 'target_version': 1,
                             'source_statement_id': 'new:1', 'target_statement_id': statement_id, 'source_event_ids': ['e']}])
    with pytest.raises(ValueError, match=error):
        store.save_memories([entry(raw, operation_id='a'), entry(operation_id='b')])
    assert store.list() == [] and store.jobs() == [] and store.operation('a') is None


def test_external_temporary_fact_is_not_mistaken_for_local_fact(tmp_path):
    store = KnowledgeStore(tmp_path)
    other = store.save_memories([entry()])[0]
    raw = package(relations=[{'kind': 'causal', 'predicate': 'causes', 'target_id': other['id'], 'target_version': 1,
                             'source_statement_id': 'new:1', 'target_statement_id': 'new:1', 'source_event_ids': ['e']}])
    with pytest.raises(ValueError, match='target fact'):
        store.save_memories([entry(raw)])
    assert len(store.list()) == 1


def test_partial_legacy_batch_replay_can_reference_already_committed_package(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = store.save_memories([entry(operation_id='already')])[0]
    raw = package(relations=[{'kind': 'causal', 'predicate': 'depends_on', 'target_id': 'new:1', 'target_version': 1,
                             'source_statement_id': 'new:1', 'target_statement_id': 'new:1', 'source_event_ids': ['e']}])
    replayed = store.save_memories([entry(first['payload'], operation_id='already'), entry(raw, operation_id='new')])
    assert replayed[0]['id'] == first['id']
    assert replayed[1]['payload']['relations'][0]['target_statement_id'] == first['payload']['statements'][0]['statement_id']
    assert len(store.list()) == 2


@pytest.mark.parametrize('explicit_shared', [False, True])
def test_same_batch_entity_identity_needs_explicit_shared_alias(tmp_path, explicit_shared):
    store = KnowledgeStore(tmp_path)
    person = {'name': '林舟', 'kind': 'person', 'entity_id': 'new:entity:1' if explicit_shared else ''}
    raw = package(statements=[{'classifications': [{'domain': 'content', 'categories': ['setting']}], 'claim': '林舟的身份', 'source_event_ids': ['e'], 'entities': [person]}])
    a, b = store.save_memories([entry(raw), entry(raw)])
    aid = a['payload']['statements'][0]['entities'][0]['entity_id']
    bid = b['payload']['statements'][0]['entities'][0]['entity_id']
    assert (aid == bid) == explicit_shared
    assert aid.startswith('ent_') and bid.startswith('ent_')


def test_conflicting_batch_entity_kind_rolls_back(tmp_path):
    store = KnowledgeStore(tmp_path)
    def raw(kind):
        return package(statements=[{'classifications': [{'domain': 'content', 'categories': ['setting']}], 'claim': '对象', 'source_event_ids': ['e'],
                              'entities': [{'name': '林舟', 'kind': kind, 'entity_id': 'new:entity:1'}]}])
    with pytest.raises(ValueError, match='entity identity conflicts'):
        store.save_memories([entry(raw('person')), entry(raw('place'))])
    assert store.list() == [] and store.entity_candidates(['林舟'], 'person') == []


def test_unpublished_old_graph_version_is_not_reintroduced_by_incoming_edge(tmp_path):
    store = KnowledgeStore(tmp_path)
    a, b = store.save_memories([entry(), entry(package(relations=[{'kind': 'causal', 'predicate': 'caused_by',
        'target_id': 'new:1', 'target_version': 1, 'source_statement_id': 'new:1', 'target_statement_id': 'new:1', 'source_event_ids': ['e']}]))])
    store.save_memories([entry(a['payload'], id=a['id'], expected_version=1)])
    current_graph = store.graph_neighborhood([(b['id'], 1)], '未知')
    assert not any(f['id'] == a['id'] and f['version'] == 1 for f in current_graph['statements'])
    assert not current_graph['edges']
    assert store.get(b['id'])['payload']['relations'][0]['target_version'] == 1


@pytest.mark.asyncio
async def test_shared_new_entity_alias_can_resolve_to_existing_identity_once(tmp_path):
    pipeline = KnowledgePipeline(None, tmp_path, None, 'test')
    store = pipeline.store('p')
    def raw(entity_id=''):
        return package(statements=[{'classifications': [{'domain': 'content', 'categories': ['setting']}], 'claim': '主角林舟', 'source_event_ids': ['e'],
                              'entities': [{'name': '林舟', 'kind': 'person', 'entity_id': entity_id}]}])
    old = store.save_memories([entry(raw())])[0]
    eid = old['payload']['statements'][0]['entities'][0]['entity_id']
    class Model:
        calls = 0
        async def chat(self, **kwargs):
            self.calls += 1
            yield SimpleNamespace(type='text_delta', content=json.dumps({'entities': [{'index': 0, 'entity_id': eid,
                'new_identity': False, 'ambiguous': False, 'reason': '明确同一人物'}]}))
    model = Model()
    pipeline.llm = model
    a, b, aliases = raw('new:entity:1'), raw('new:entity:1'), {}
    await pipeline._resolve_entities('p', a, evidence(), aliases)
    await pipeline._resolve_entities('p', b, evidence(), aliases)
    assert model.calls == 1 and a['statements'][0]['entities'][0]['entity_id'] == eid
    assert b['statements'][0]['entities'][0]['entity_id'] == eid


def test_split_merge_withdrawal_have_exact_versioned_fact_lineage(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = store.save_memories([entry()])[0]
    old_id = first['payload']['statements'][0]['statement_id']
    split = package(statements=[{'classifications': [{'domain': 'writing', 'categories': ['polishing']}], 'claim': text, 'source_event_ids': ['e']}
                           for text in ('动作承接', '信息承接')], statement_changes=[{'kind': 'split',
        'previous_statement_ids': [old_id], 'current_statement_ids': ['new:1', 'new:2'], 'reason': '区分两种承接', 'source_event_ids': ['e']}])
    second = store.save_memories([entry(split, id=first['id'], expected_version=1)])[0]
    ids = [f['statement_id'] for f in second['payload']['statements']]
    assert len(store.statement_history(first['id'], old_id)['changes']) == 2
    merged = package(statement_changes=[{'kind': 'merged', 'previous_statement_ids': ids, 'current_statement_ids': ['new:1'],
                                   'reason': '综合条件', 'source_event_ids': ['e']}])
    third = store.save_memories([entry(merged, id=first['id'], expected_version=2)])[0]
    current = third['payload']['statements'][0]['statement_id']
    withdrawn = deepcopy(third['payload'])
    withdrawn['statements'][0]['status'] = 'withdrawn'
    withdrawn['statement_changes'] = [{'kind': 'withdrawn', 'previous_statement_ids': [current], 'reason': '人类否定', 'source_event_ids': ['e']}]
    fourth = store.save_memories([entry(withdrawn, id=first['id'], expected_version=3)])[0]
    assert store.statement_history(first['id'], current)['changes'][-1]['current_statement_id'] == ''
    assert not store.graph()['statements'] and not store.graph()['links']
    assert store.get(first['id'], 1)['payload']['statements'][0]['status'] == 'active'
    assert fourth['payload']['statements'][0]['status'] == 'withdrawn'


def test_missing_fact_governance_or_invented_previous_fact_rejected(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = store.save_memories([entry()])[0]
    with pytest.raises(ValueError, match='explicit withdrawal'):
        store.save_memories([entry(id=first['id'], expected_version=1)])
    invalid = package(statement_changes=[{'kind': 'corrected', 'previous_statement_ids': ['invented'], 'current_statement_ids': ['new:1'],
                                    'reason': '修改', 'source_event_ids': ['e']}])
    with pytest.raises(ValueError, match='unknown versioned fact'):
        store.save_memories([entry(invalid, id=first['id'], expected_version=1)])
    assert store.get(first['id'])['version'] == 1


def test_changed_fact_retains_identity_only_with_correction_evidence(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = store.save_memories([entry()])[0]
    raw = deepcopy(first['payload'])
    fid = raw['statements'][0]['statement_id']
    raw['statements'][0]['claim'] = '根据场景调整承接'
    with pytest.raises(ValueError, match='explicit correction'):
        store.save_memories([entry(raw, id=first['id'], expected_version=1)])
    raw['statement_changes'] = [{'kind': 'corrected', 'previous_statement_ids': [fid], 'current_statement_ids': [fid],
                            'reason': '补充适用条件', 'source_event_ids': ['e']}]
    second = store.save_memories([entry(raw, id=first['id'], expected_version=1)])[0]
    lineage = store.statement_history(first['id'], fid)
    assert [r['version'] for r in lineage['versions']] == [1, 2]
    assert lineage['changes'][0]['kind'] == 'corrected'
    assert second['payload']['statements'][0]['statement_id'] == fid


def test_withdrawn_fact_cannot_support_new_insight_but_pinned_old_version_remains(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = store.save_memories([entry()])[0]
    old = publish(store, first)
    raw = deepcopy(first['payload'])
    fid = raw['statements'][0]['statement_id']
    raw['statements'][0]['status'] = 'withdrawn'
    raw['statement_changes'] = [{'kind': 'withdrawn', 'previous_statement_ids': [fid], 'reason': '明确否定', 'source_event_ids': ['e']}]
    second = store.save_memories([entry(raw, id=first['id'], expected_version=1)])[0]
    payload = deepcopy(old['payload'])
    payload['sources'][0]['version'] = 2
    with pytest.raises(ValueError, match='Withdrawn statements'):
        store.publish_insights([entry(payload)], {first['id']: 2})
    assert any(f['id'] == first['id'] and f['version'] == 1 for f in store.graph()['statements'])
    assert not any(f['id'] == first['id'] and f['version'] == 2 for f in store.graph()['statements'])
    assert second['version'] == 2


def test_recall_score_promotes_at_200_without_resetting_on_version_update(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    insight = publish(store, memory)
    assert store.view(insight)['weight'] == 65
    for task in range(4):
        store.mark_used(insight['id'], 1, str(task), '', 'main')
    assert store.view(store.get(insight['id']))['weight'] == 125
    updated = publish(store, memory, id=insight['id'], expected_version=1)
    store.mark_used(updated['id'], 2, '0', '', 'reviewer')
    assert store.view(store.get(insight['id']))['weight'] == 125
    for task in range(4, 9):
        store.mark_used(updated['id'], 2, str(task), '', 'main')
    assert store.get(insight['id'])['layer'] == 'pattern'
    assert store.view(store.get(insight['id']))['weight'] == 200
    assert publish(store, memory, id=insight['id'], expected_version=2)['layer'] == 'pattern'


def test_fusion_preserves_max_score_not_sum_and_next_use_adds_once(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    a, b = publish(store, memory), publish(store, memory)
    for index in range(4):
        store.mark_used(a['id'], 1, str(index), '', 'main')
    store.mark_used(b['id'], 1, 'b', '', 'main')
    merged = publish(store, memory, replaces=[a['id'], b['id']], replacement_reason='综合')
    assert store.view(merged)['weight'] == 125
    assert store.get(a['id'])['status'] == 'withdrawn'
    store.mark_used(merged['id'], 1, '0', '', 'main')
    assert store.view(store.get(merged['id']))['weight'] == 125
    store.mark_used(merged['id'], 1, 'new', '', 'main')
    assert store.view(store.get(merged['id']))['weight'] == 140


def test_resumed_task_dedup_reverses_only_duplicate_recall_points(tmp_path):
    store = KnowledgeStore(tmp_path)
    insight = publish(store, store.save_memories([entry()])[0])
    store.mark_used(insight['id'], 1, 'old', '', 'main')
    store.mark_used(insight['id'], 1, 'new', '', 'reviewer')
    assert store.view(store.get(insight['id']))['weight'] == 95
    store.merge_task_uses('root', ['old', 'new'])
    assert store.view(store.get(insight['id']))['weight'] == 80
    assert store.view(store.get(insight['id']))['support_count'] == 1


def test_fusion_into_existing_identity_preserves_score(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    target, other = publish(store, memory), publish(store, memory)
    for index in range(8):
        store.mark_used(target['id'], 1, str(index), '', 'main')
    store.mark_used(other['id'], 1, 'other', '', 'main')
    updated = publish(store, memory, id=target['id'], expected_version=1, replaces=[other['id']])
    assert store.view(updated)['weight'] == 185 and updated['layer'] == 'insight'
    store.mark_used(updated['id'], 2, 'other', '', 'main')
    assert store.view(store.get(updated['id']))['weight'] == 185
    store.mark_used(updated['id'], 2, 'next', '', 'main')
    assert store.get(updated['id'])['layer'] == 'pattern'


def test_score_cap_and_inactive_knowledge_cannot_gain_points(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    insight = publish(store, memory)
    for index in range(8):
        store.mark_used(insight['id'], 1, str(index), '', 'main', score_threshold=500)
    withdrawn_payload = {**insight['payload'], 'status': 'disputed'}
    disputed = store.publish_insights([entry(withdrawn_payload, id=insight['id'], expected_version=1)], {memory['id']: 1})[0]
    assert store.mark_used(disputed['id'], 2, 'blocked', '', 'main') == 0
    assert store.view(disputed)['weight'] == 185
    active = publish(store, memory, id=insight['id'], expected_version=2)
    for index in range(8, 25):
        store.mark_used(active['id'], 3, str(index), '', 'main', score_threshold=500)
    assert store.view(store.get(active['id']))['weight'] == 300


@pytest.mark.asyncio
async def test_pipeline_saves_cross_package_graph_before_learning_and_retry_is_free(tmp_path):
    class Embedding:
        async def encode(self, texts):
            return np.asarray([[1., 0.] for _ in texts])
    class Model:
        calls = []
        async def chat(self, **kwargs):
            self.calls.append(kwargs['tag'])
            if kwargs['tag'].endswith('retain'):
                one = package(relations=[{'kind': 'causal', 'predicate': 'causes', 'target_id': 'new:2',
                    'target_version': 1, 'source_statement_id': 'new:1', 'target_statement_id': 'new:1', 'source_event_ids': ['e']}])
                response = {'memories': [one, package(title='信息承接', direction='信息承接')]}
            else:
                response = {'insights': [], 'withdrawals': []}
            yield SimpleNamespace(type='text_delta', content=json.dumps(quoted_memory_response(response, kwargs['messages'])))
    model = Model()
    pipeline = KnowledgePipeline(model, tmp_path, Embedding(), 'test')
    unit = {'history_id': 'h', 'kind': 'requirement', 'status': 'completed', 'events': evidence()}
    await pipeline.process('p', unit)
    count = len(model.calls)
    await pipeline.process('p', unit)
    assert len(model.calls) == count
    assert len(pipeline.store('p').list(('memory',))) == 2
    assert len(pipeline.store('p').graph()['edges']) == 1


def test_matching_keeps_new_fact_alias_when_existing_facts_are_prepended(tmp_path):
    store = KnowledgeStore(tmp_path)
    old = store.save_memories([entry()])[0]
    incoming = KnowledgePipeline._validate_package(package(relations=[{'kind': 'causal', 'predicate': 'supports',
        'target_id': 'new:2', 'target_version': 1, 'source_statement_id': 'new:1', 'target_statement_id': 'new:1',
        'source_event_ids': ['e']}]), evidence())
    incoming['statements'] = [*old['payload']['statements'], *incoming['statements']]
    updated, other = store.save_memories([entry(incoming, id=old['id'], expected_version=1), entry()])
    relation = updated['payload']['relations'][0]
    assert relation['source_statement_id'] == updated['payload']['statements'][1]['statement_id']
    assert relation['target_statement_id'] == other['payload']['statements'][0]['statement_id']


@pytest.mark.asyncio
async def test_explicit_retry_api_retries_failed_jobs_but_does_not_auto_start_models(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from novelagent.server.routes.traces import router
    (tmp_path / 'p').mkdir()
    calls, scheduled = [], []
    class Pipeline:
        def store(self, project_id):
            return SimpleNamespace(jobs=lambda: [{'status': 'failed'}])
        async def consolidate_pending(self, project_id, *, retry_failed=False):
            calls.append((project_id, retry_failed))
    app = FastAPI()
    app.include_router(router)
    app.state.agent_loop = SimpleNamespace(working_dir=str(tmp_path))
    app.state.knowledge_runtime = SimpleNamespace(pipeline=Pipeline())
    app.state.agent_trace_tasks = SimpleNamespace(submit=lambda task, **kwargs: scheduled.append(task))
    with TestClient(app) as client:
        assert client.post('/api/projects/p/knowledge/retry-pending').json() == {'scheduled': 1}
    assert calls == []
    await scheduled[0]
    assert calls == [('p', True)]


def test_fact_lineage_api_reads_exact_fact_history(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from novelagent.server.routes.traces import router
    store = KnowledgeStore(tmp_path / 'p')
    memory = store.save_memories([entry()])[0]
    fid = memory['payload']['statements'][0]['statement_id']
    app = FastAPI()
    app.include_router(router)
    app.state.agent_loop = SimpleNamespace(working_dir=str(tmp_path))
    with TestClient(app) as client:
        response = client.get(f"/api/projects/p/knowledge/{memory['id']}/statements/{fid}")
        assert response.status_code == 200 and response.json()['versions'][0]['version'] == 1
        assert client.get(f"/api/projects/p/knowledge/{memory['id']}/facts/{fid}").json() == response.json()
        assert client.get(f"/api/projects/p/knowledge/{memory['id']}/statements/missing").status_code == 404


def test_old_payload_defaults_are_read_compatibly_without_rewriting_versions(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    insight = publish(store, memory)
    legacy = deepcopy(insight['payload'])
    for key in ('times', 'authority', 'human_confirmed', 'solutions'):
        legacy.pop(key)
    raw = json.dumps(legacy, ensure_ascii=False)
    with store.connect() as db:
        db.execute('UPDATE versions SET payload=? WHERE id=?', (raw, insight['id']))
    loaded = store.get(insight['id'])
    assert loaded['payload']['times'] == [] and loaded['payload']['authority'] == 'agent'
    assert store.view(loaded)['content']
    with store.connect() as db:
        assert db.execute('SELECT payload FROM versions WHERE id=?', (insight['id'],)).fetchone()[0] == raw


def test_invalid_legacy_structure_is_readable_but_not_recalled(tmp_path):
    store = KnowledgeStore(tmp_path)
    memory = store.save_memories([entry()])[0]
    insight = publish(store, memory)
    legacy = deepcopy(insight['payload'])
    legacy['content_types'] = ['outline']
    with store.connect() as db:
        db.execute('UPDATE versions SET payload=? WHERE id=?', (json.dumps(legacy), insight['id']))
    loaded = store.get(insight['id'])
    assert loaded['status'] == 'validation_failed' and loaded['schema_errors']
    assert store.list(('insight',)) == []
    assert store.view(store.list(('insight',), include_inactive=True)[0])['content'].startswith('此历史知识')
