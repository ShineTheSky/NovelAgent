"""ID + quote verification, deterministic repair and diagnostic retention; no API."""

from copy import deepcopy
import json

import pytest

from novelagent.llm.client import LLMResponse
from novelagent.memory.consolidation import KnowledgePipeline, MemoryValidationError
from novelagent.memory.models import MemoryExtractionPackage
from novelagent.memory.extraction_prompts import MEMORY_RETAIN_PROMPT, MEMORY_MATCH_PROMPT
from tests.test_memory_extraction_prompts import package, unit, pipeline_for


def quoted_package(event='ev', excerpt='仅首个副本低魔'):
    raw = package(event=event)
    raw['source_event_quotes'] = [{'event_id': event, 'excerpt': excerpt}]
    return raw


def checked(tmp_path, raw, events, repairs=None):
    return KnowledgePipeline(None, tmp_path, None, 'test')._checked_model_package(
        'p', raw, events, {'world/a.md'}, 'memories[0]', require_event_quotes=True, corrections=repairs)


def test_quote_schema_and_prompts_require_relevant_original_text():
    schema = MemoryExtractionPackage.model_json_schema()
    assert 'source_event_quotes' in schema['required']
    assert set(schema['$defs']['SourceEventQuote']['required']) == {'event_id', 'excerpt'}
    for prompt in (MEMORY_RETAIN_PROMPT, MEMORY_MATCH_PROMPT):
        assert '不固定截取前 10 个字符' in prompt
        assert '只有唯一匹配才能纠正 ID' in prompt
        assert '不能跨字段或跨事件拼接' in prompt
        assert '达到 90% 即可通过' in prompt
        assert '与 source_event_quotes 中的 ID 清单核对' in prompt


@pytest.mark.parametrize('wrong_id', ['typo', 'other'])
def test_missing_or_wrong_valid_id_is_repaired_across_all_reference_fields(tmp_path, wrong_id):
    raw = quoted_package(wrong_id)
    raw['relations'] = [{'kind': 'semantic', 'predicate': 'related', 'target_version': 1,
                         'source_event_ids': [wrong_id]}]
    raw['statement_changes'] = [{'kind': 'corrected', 'previous_statement_ids': ['old'],
        'current_statement_ids': ['new:1'], 'reason': '纠正旧理解', 'source_event_ids': [wrong_id]}]
    events = [*unit()['events'], {'event_id': 'other', 'kind': 'user_input', 'content': '保持人物年龄'}]
    original, repairs = deepcopy(raw), []
    saved = checked(tmp_path, raw, events, repairs)
    assert raw == original and 'source_event_quotes' not in saved
    for key in ('statements', 'relations', 'statement_changes'):
        assert saved[key][0]['source_event_ids'] == ['ev']
    assert repairs[0]['supplied_id'] == wrong_id and repairs[0]['corrected_id'] == 'ev'


def test_valid_id_with_duplicate_text_is_not_guessed_or_changed(tmp_path):
    events = [*unit()['events'], {'event_id': 'another', 'kind': 'user_input', 'content': '仅首个副本低魔'}]
    repairs = []
    assert checked(tmp_path, quoted_package(), events, repairs)['statements'][0]['source_event_ids'] == ['ev']
    assert repairs == []


def test_ninety_percent_quote_can_repair_id_without_changing_original_output(tmp_path):
    text = '只改 `chapters/` 正文和 `characters/` 人物卡，其余保持原样。'
    raw = quoted_package('typo', text.replace('`', ''))
    events = [{'event_id': 'real', 'kind': 'main_delegation', 'content': text}]
    original, repairs = deepcopy(raw), []
    saved = checked(tmp_path, raw, events, repairs)
    assert saved['statements'][0]['source_event_ids'] == ['real']
    assert raw == original
    assert repairs[0]['corrected_id'] == 'real'


def test_multiple_ninety_percent_sources_remain_ambiguous(tmp_path):
    raw = quoted_package('typo', 'abcdefghij')
    events = [{'event_id': eid, 'kind': 'user_input', 'content': text}
              for eid, text in [('one', 'abcdefghix'), ('two', 'abcdefghiY')]]
    with pytest.raises(MemoryValidationError) as error:
        checked(tmp_path, raw, events)
    assert error.value.details['status'] == 'ambiguous'
    assert error.value.details['minimum_similarity'] == .90
    assert len(error.value.details['candidates']) == 2


@pytest.mark.parametrize('count,status', [(0, 'missing'), (2, 'ambiguous')])
def test_no_unique_excerpt_reports_exact_candidates_and_never_guesses(tmp_path, count, status):
    events = [{'event_id': f'ev{i}', 'kind': 'user_input', 'content': '仅首个副本低魔'} for i in range(count)]
    with pytest.raises(MemoryValidationError) as error:
        checked(tmp_path, quoted_package('typo'), events)
    details = error.value.details
    assert details['location'] == 'memories[0].source_event_quotes[0]'
    assert details['status'] == status and details['supplied_id'] == 'typo'
    assert len(details['candidates']) == count


@pytest.mark.parametrize('quotes,missing,unused', [([], ['ev'], []),
    ([{'event_id': 'ev', 'excerpt': '仅首个副本低魔'}, {'event_id': 'extra', 'excerpt': '额外'}], [], ['extra'])])
def test_quote_table_covers_only_the_cited_ids(tmp_path, quotes, missing, unused):
    raw = quoted_package()
    raw['source_event_quotes'] = quotes
    with pytest.raises(MemoryValidationError) as error:
        checked(tmp_path, raw, unit()['events'])
    assert error.value.details['missing_ids'] == missing
    assert error.value.details['unused_ids'] == unused


def test_one_supplied_id_cannot_be_repaired_to_two_different_events(tmp_path):
    raw = quoted_package('typo')
    raw['source_event_quotes'].append({'event_id': 'typo', 'excerpt': '保持人物年龄'})
    events = [*unit()['events'], {'event_id': 'other', 'kind': 'user_input', 'content': '保持人物年龄'}]
    with pytest.raises(MemoryValidationError, match='different events'):
        checked(tmp_path, raw, events)


def test_typography_and_decoded_ask_answer_quote_can_repair_an_id(tmp_path):
    raw = quoted_package('typo', "人物:'九叔',地点[村庄]...")
    events = [{'event_id': 'answer', 'kind': 'user_answer', 'content': json.dumps([
        {'answer': '人物：“九叔”，地点【村庄】……'}])}]
    assert checked(tmp_path, raw, events)['statements'][0]['source_event_ids'] == ['answer']


def test_fragment_repair_filters_failed_edits_and_non_document_events(tmp_path):
    raw = quoted_package()
    raw['artifact_paths'] = ['world/a.md']
    raw['fragments'] = [{'source_event_id': 'typo', 'before': '', 'after': '相关片段'}]
    raw['source_event_quotes'].append({'event_id': 'typo', 'excerpt': '相关片段'})
    events = [*unit()['events'], {'event_id': 'user', 'kind': 'user_input', 'content': '相关片段'}]
    for event_id, success in [('bad', False), ('good', True)]:
        events.append({'event_id': event_id, 'kind': 'file_change_attempt', 'content': json.dumps({
            'tool': 'Write', 'success': success, 'params': {'path': 'world/a.md', 'content': '相关片段'},
            'result': '当前修订: v2'})})
    repairs = []
    result = checked(tmp_path, raw, events, repairs)
    assert result['fragments'][0]['source_event_id'] == 'good'
    assert result['fragments'][0]['revision_id'] == 'v2'
    assert repairs[0]['corrected_id'] == 'good'


class RawModel:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        yield LLMResponse(type='text_delta', content=json.dumps(
            self.responses[min(len(self.calls)-1, len(self.responses)-1)], ensure_ascii=False))


class Recorder:
    def __init__(self):
        self.calls = []

    async def capture(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_repair_keeps_raw_trace_records_badcase_and_saves_only_canonical_id(tmp_path, monkeypatch):
    from novelagent.storage import database
    from novelagent.trace.store import TraceStore
    monkeypatch.setattr(database, 'DB_PATH', str(tmp_path / 'trace.sqlite3'))
    await database.init()
    trace_store = TraceStore()
    model = RawModel([{'memories': [quoted_package('typo')]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    pipeline.trace_store = trace_store
    recorder = Recorder()
    pipeline.bad_cases = recorder
    await pipeline.process('p', unit('completed'))
    assert len(model.calls) == 1 and len(recorder.calls) == 1
    assert recorder.calls[0]['params']['error_kind'] == 'reference_correction'
    assert recorder.calls[0]['params']['supplied_id'] == 'typo'
    assert recorder.calls[0]['params']['corrected_id'] == 'ev'
    assert recorder.calls[0]['params']['location'] == 'memories[0].source_event_quotes[0]'
    saved = pipeline.store('p').list(('memory',))[0]
    assert saved['payload']['statements'][0]['source_event_ids'] == ['ev']
    assert 'source_event_quotes' not in saved['payload']
    traces = await trace_store.list_project_traces('p')
    events = await trace_store.list_events(traces[0]['trace_id'])
    assert any(e['event_type'] == 'text_delta' and 'typo' in json.dumps(e['payload']) for e in events)
    assert any(e['event_type'] == 'event_reference_correction' and e['payload']['corrected_id'] == 'ev' for e in events)
    with pipeline.store('p').connect() as db:
        cached = json.loads(db.execute('SELECT response FROM model_results').fetchone()[0])
    assert cached['memories'][0]['source_event_quotes'][0]['event_id'] == 'typo'


@pytest.mark.asyncio
async def test_ambiguous_quote_gets_specific_feedback_and_can_be_repaired(tmp_path, monkeypatch):
    source = unit('completed')
    source['events'] = [{'event_id': 'first', 'history_id': 'h', 'kind': 'user_input', 'content': '共同要求，首个副本低魔'},
        {'event_id': 'second', 'history_id': 'h', 'kind': 'user_input', 'content': '共同要求，其他副本不同'}]
    model = RawModel([{'memories': [quoted_package('typo', '共同要求')]},
        {'memories': [quoted_package('first', '首个副本低魔')]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    recorder = Recorder()
    pipeline.bad_cases = recorder
    await pipeline.process('p', source)
    assert len(model.calls) == 2 and len(recorder.calls) == 1
    details = json.loads(model.calls[1]['messages'][-1]['content'].split('\n校验详情：', 1)[1])
    assert details['status'] == 'ambiguous'
    assert {c['event_id'] for c in details['candidates']} == {'first', 'second'}
    assert pipeline.store('p').list(('memory',))[0]['payload']['statements'][0]['source_event_ids'] == ['first']


@pytest.mark.asyncio
async def test_missing_quote_table_is_retried_until_max_turns_not_silently_accepted(tmp_path, monkeypatch):
    model = RawModel([{'memories': [package()]}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch)
    pipeline.config['analysis_max_turns'] = 2
    recorder = Recorder()
    pipeline.bad_cases = recorder
    with pytest.raises(RuntimeError, match='max_turns=2'):
        await pipeline.process('p', unit('completed'))
    assert len(model.calls) == 2 and len(recorder.calls) == 3
    assert recorder.calls[0]['params']['errors'][0]['location'] == ['source_event_quotes']
    assert pipeline.store('p').list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['new', 'update'])
async def test_match_repair_never_borrows_an_unselected_candidates_evidence(tmp_path, monkeypatch, action):
    pipeline = pipeline_for(tmp_path, None, monkeypatch)
    store = pipeline.store('p')
    chosen = store.save_memory(package(event='old'), [{'event_id': 'old', 'kind': 'user_input',
        'content': '所选候选的原文'}], [1, 0], 'test-model')
    outside = store.save_memory(package(event='outside'), [{'event_id': 'outside', 'kind': 'user_input',
        'content': '未选中候选的专有原文'}], [1, 0], 'test-model')
    model = RawModel([{'memories': [quoted_package()]}, {'action': action,
        'memory_id': chosen['id'] if action == 'update' else '', 'expected_version': 1 if action == 'update' else 0,
        'memory': quoted_package('typo', '未选中候选的专有原文')}])
    pipeline = pipeline_for(tmp_path, model, monkeypatch, [chosen, outside])
    pipeline.config['analysis_max_turns'] = 1
    recorder = Recorder()
    pipeline.bad_cases = recorder
    with pytest.raises(RuntimeError, match='no unique allowed source'):
        await pipeline.process('p', unit('completed'))
    assert len(model.calls) == 2
    assert recorder.calls[0]['params']['status'] == 'missing'
    assert store.get(chosen['id'])['version'] == store.get(outside['id'])['version'] == 1


@pytest.mark.asyncio
async def test_cached_raw_reference_is_rechecked_repaired_and_traced_without_new_call(tmp_path, monkeypatch):
    from novelagent.storage import database
    from novelagent.trace.store import TraceStore
    monkeypatch.setattr(database, 'DB_PATH', str(tmp_path / 'trace.sqlite3'))
    await database.init()
    model = RawModel([{'memories': [quoted_package('typo')]}])
    pipeline = KnowledgePipeline(model, tmp_path, None, 'test', trace_store=TraceStore())
    recorder = Recorder()
    pipeline.bad_cases = recorder
    def validate(result):
        repairs = []
        pipeline._checked_model_package('p', result['memories'][0], unit()['events'], set(), 'memories[0]', corrections=repairs)
        return repairs
    for _ in range(2):
        result = await pipeline._call('p', '离线缓存测试', 'retain', validate=validate, history_id='h')
        assert result['memories'][0]['source_event_quotes'][0]['event_id'] == 'typo'
    assert len(model.calls) == 1 and len(recorder.calls) == 2
    traces = await pipeline.trace_store.list_project_traces('p')
    assert len(traces) == 2
    all_events = [event for trace in traces for event in await pipeline.trace_store.list_events(trace['trace_id'])]
    assert sum(e['event_type'] == 'event_reference_correction' for e in all_events) == 2
    assert any(e['event_type'] == 'cached_response' for e in all_events)
