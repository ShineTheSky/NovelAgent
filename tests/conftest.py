import pytest
import tempfile
import os
import json
from copy import deepcopy
from pathlib import Path


def quoted_memory_response(response, messages):
    """Upgrade synthetic model fixtures to the event-quote output contract."""
    result = deepcopy(response)
    packages = result.get('memories', []) if isinstance(result, dict) else []
    if isinstance(result, dict) and result.get('memory') is not None:
        packages = [result['memory']]
    if not packages:
        return result
    prompt = next(m['content'] for m in messages if isinstance(m.get('content'), str)
                  and ('\nHistory:' in m['content'] or '\nNewEvidence:' in m['content']))
    if '\nHistory:' in prompt:
        events = json.loads(prompt.split('\nHistory:', 1)[1])['events']
    else:
        source, candidates = prompt.split('\nNewEvidence:', 1)[1].split('\nCandidates:', 1)
        events = json.loads(source)
        if result.get('action') == 'update':
            events += next(c['evidence'] for c in json.loads(candidates) if c['id'] == result['memory_id'])
    by_id = {e['event_id']: e for e in events}
    def excerpt(event):
        content = event.get('content', '__missing_event_quote__')
        try:
            value = json.loads(content)
        except ValueError:
            return content
        def first_text(value):
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                params = value.get('params', {})
                for key in ('task', 'old_string', 'new_string', 'content'):
                    if isinstance(params.get(key), str):
                        return params[key]
                return next((text for item in value.values() if (text := first_text(item))), '')
            if isinstance(value, list):
                return next((text for item in value if (text := first_text(item))), '')
            return ''
        return first_text(value) or content
    for package in packages:
        if 'source_event_quotes' in package:
            continue
        ids = {eid for key in ('statements', 'relations', 'statement_changes')
               for row in package.get(key, []) for eid in row['source_event_ids']}
        ids.update(f['source_event_id'] for f in package.get('fragments', []))
        package['source_event_quotes'] = [{'event_id': eid,
            'excerpt': excerpt(by_id.get(eid, {}))} for eid in sorted(ids)]
    return result


@pytest.fixture(autouse=True)
def block_external_http(monkeypatch):
    """Model tests must use mocks; ASGI/TestClient transports remain available."""
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests")

    async def blocked_async(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d

@pytest.fixture
def working_dir(temp_dir):
    wd = Path(temp_dir) / "workspace"
    wd.mkdir()
    chapters = wd / "chapters"
    chapters.mkdir()
    (chapters / "ch01.md").write_text("# Chapter 1\nHello world\n张三走进了房间。", encoding="utf-8")
    (chapters / "ch02.md").write_text("# Chapter 2\n张三发现了秘密。\n李四在暗中观察。", encoding="utf-8")
    return str(wd)

@pytest.fixture(autouse=True)
def setup_llm_env():
    os.environ.setdefault('ANTHROPIC_API_KEY', 'test-key')
    os.environ.setdefault('DEEPSEEK_API_KEY', 'test-key')
