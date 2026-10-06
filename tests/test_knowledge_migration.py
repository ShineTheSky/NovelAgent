import json
from types import SimpleNamespace

import numpy as np
import pytest

from novelagent.history.store import DocumentHistoryStore
from novelagent.history.analyzer import HistoryAnalyzer
from novelagent.memory.models import MemoryPackage
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.migration import KnowledgeMigration, preview


class Embedding:
    async def encode(self, texts):
        return np.asarray([[1., 0., 0.] for _ in texts], dtype=np.float32)


def setup_project(tmp_path):
    project = tmp_path / 'p'
    history = DocumentHistoryStore(project)
    hid = history.start_request('old', 'session', '保持自然承接')
    history.close_request('old', 'completed', '收到')
    history.set_analysis_status(hid, 'complete')  # legacy extraction must not block explicit migration
    legacy = project / '.memory/project/old.md'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(f'---\nid: old-memory\nhistory_ids: [{hid}]\nhistory_event_ids: [{hid}:input]\n---\n原记忆', encoding='utf-8')
    return project, history, hid, legacy


def files(project):
    return {str(p.relative_to(project)): p.read_bytes() for p in project.rglob('*') if p.is_file()}


def test_preview_is_read_only_and_deduplicates_sources(tmp_path):
    project, history, hid, legacy = setup_project(tmp_path)
    duplicate = project / '.insight/old.md'
    duplicate.parent.mkdir()
    duplicate.write_text(f'---\nid: old-insight\nhistory_ids: [{hid}]\n---\n旧洞察', encoding='utf-8')
    before = files(project)
    report = preview(project)
    assert files(project) == before and not (project / '.knowledge').exists()
    assert len(report['targets']) == 1 and report['targets'][0]['history_id'] == hid
    assert report['targets'][0]['eligible']


class LLM:
    def __init__(self, event_id):
        self.event_id = event_id
        self.tags = []
        self.fail_learning = False

    async def chat(self, **kwargs):
        self.tags.append(kwargs['tag'])
        if kwargs['tag'].endswith('retain'):
            package = MemoryPackage.model_validate({'title': '承接', 'direction': '承接',
                'background': '原有段落', 'requirement': '保持自然承接',
                'facts': [{'domain': 'writing', 'claim': '保持自然承接', 'source_event_ids': [self.event_id]}]}).model_dump()
            yield SimpleNamespace(type='text_delta', content=json.dumps({'memories': [package]}))
        elif self.fail_learning:
            yield SimpleNamespace(type='error', error='暂时失败')
        else:
            yield SimpleNamespace(type='text_delta', content=json.dumps({'insights': [], 'withdrawals': []}))


def runner(tmp_path, hid):
    llm = LLM(hid + ':input')
    pipeline = KnowledgePipeline(llm, tmp_path, Embedding(), 'model')
    analyzer = HistoryAnalyzer(llm, str(tmp_path), None, knowledge_pipeline=pipeline)
    return KnowledgeMigration(analyzer), llm


@pytest.mark.asyncio
async def test_explicit_migration_forces_legacy_completed_history_and_is_idempotent(tmp_path):
    project, history, hid, legacy = setup_project(tmp_path)
    migration, llm = runner(tmp_path, hid)
    before = legacy.read_bytes()
    result = await migration.run('p', [hid])
    assert result[0]['status'] == 'complete' and len(result[0]['memories']) == 1
    calls = len(llm.tags)
    await migration.run('p', [hid])
    assert len(llm.tags) == calls and legacy.read_bytes() == before
    report = preview(project)
    assert report['targets'][0]['progress']['status'] == 'complete'
    assert report['source_mappings'][0]['match'] == 'exact_event_overlap'
    assert report['source_mappings'][0]['confirmed_replacement'] is False


@pytest.mark.asyncio
async def test_learning_failure_is_not_reported_as_complete_and_retry_keeps_memory(tmp_path):
    project, history, hid, legacy = setup_project(tmp_path)
    migration, llm = runner(tmp_path, hid)
    llm.fail_learning = True
    assert (await migration.run('p', [hid]))[0]['status'] == 'learning_pending'
    memory = migration.pipeline.store('p').list(('memory',))[0]
    llm.fail_learning = False
    await migration.recover('p')
    assert preview(project)['targets'][0]['progress']['status'] == 'complete'
    assert migration.pipeline.store('p').list(('memory',))[0]['id'] == memory['id']
    assert llm.tags.count(':knowledge/retain') == 1


@pytest.mark.asyncio
async def test_recovery_does_not_authorize_unrequested_legacy_migration(tmp_path):
    project, history, hid, legacy = setup_project(tmp_path)
    migration, llm = runner(tmp_path, hid)
    await migration.recover('p')
    assert llm.tags == [] and not (project / '.knowledge').exists()


@pytest.mark.parametrize('ids', [[], ['missing'], ['a', 'b', 'c', 'd', 'e', 'f']])
def test_migration_requires_bounded_eligible_ids(tmp_path, ids):
    project, history, hid, legacy = setup_project(tmp_path)
    migration, _ = runner(tmp_path, hid)
    with pytest.raises(ValueError, match='eligible'):
        migration.validate('p', ids)


def test_migration_api_requires_explicit_model_call_confirmation(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from novelagent.server.routes.traces import router
    project, history, hid, legacy = setup_project(tmp_path)
    migration, llm = runner(tmp_path, hid)
    app = FastAPI()
    app.include_router(router)
    app.state.agent_loop = SimpleNamespace(working_dir=str(tmp_path), history_analyzer=migration.analyzer)
    with TestClient(app) as client:
        assert client.get('/api/projects/p/knowledge-migration').status_code == 200
        assert client.post('/api/projects/p/knowledge-migration', json={'history_ids': [hid]}).status_code == 422
        assert client.post('/api/projects/p/knowledge-migration', json={'history_ids': ['missing'], 'confirm_model_calls': True}).status_code == 422
    assert llm.tags == []
