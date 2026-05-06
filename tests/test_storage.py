import pytest
from novelagent.storage.database import init, get_connection, get_db_path
from novelagent.storage import models


@pytest.mark.asyncio
async def test_db_init():
    await init()
    path = await get_db_path()
    assert path.endswith('novelagent.db')

@pytest.mark.asyncio
async def test_create_session():
    s = await models.create_session('proj-test')
    assert s.session_id
    assert s.project_id == 'proj-test'

@pytest.mark.asyncio
async def test_save_and_load_messages():
    s = await models.create_session('proj-test')
    msgs = [{'role': 'user', 'content': 'hello'}]
    await models.save_messages(s.session_id, msgs, 5)
    loaded = await models.load_session(s.session_id)
    assert loaded.get_messages() == msgs
    assert loaded.token_count == 5

@pytest.mark.asyncio
async def test_load_nonexistent_session():
    s = await models.load_session('nonexistent')
    assert s is None

@pytest.mark.asyncio
async def test_complex_message_serialization():
    s = await models.create_session('proj-test')
    msgs = [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': '1', 'name': 'Grep', 'input': {'pattern': 'test'}}
        ]},
        {'role': 'tool_result', 'tool_call_id': '1', 'content': 'found 3'}
    ]
    await models.save_messages(s.session_id, msgs, 20)
    loaded = await models.load_session(s.session_id)
    assert loaded.get_messages() == msgs

@pytest.mark.asyncio
async def test_create_and_list_projects():
    p = await models.create_project('test-proj', 'scifi')
    try:
        assert p.name == 'test-proj'
        assert p.genre == 'scifi'
        projects = await models.list_projects()
        assert any(pr.name == 'test-proj' for pr in projects)
    finally:
        conn = await get_connection()
        await conn.execute("DELETE FROM sessions WHERE project_id = ?", (p.project_id,))
        await conn.execute("DELETE FROM projects WHERE project_id = ?", (p.project_id,))
        await conn.commit()
        await conn.close()

@pytest.mark.asyncio
async def test_update_word_count():
    p = await models.create_project('wc-test')
    try:
        await models.update_word_count(p.project_id, 5000)
        projects = await models.list_projects()
        found = next(pr for pr in projects if pr.project_id == p.project_id)
        assert found.word_count == 5000
    finally:
        conn = await get_connection()
        await conn.execute("DELETE FROM sessions WHERE project_id = ?", (p.project_id,))
        await conn.execute("DELETE FROM projects WHERE project_id = ?", (p.project_id,))
        await conn.commit()
        await conn.close()

@pytest.mark.asyncio
async def test_list_sessions_filtered():
    s1 = await models.create_session('proj-a')
    s2 = await models.create_session('proj-b')
    sessions = await models.list_sessions('proj-a')
    assert all(s.project_id == 'proj-a' for s in sessions)

@pytest.fixture
async def db_created():
    await init()
