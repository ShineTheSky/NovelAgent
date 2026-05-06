"""数据模型与CRUD操作"""

import json
import uuid
from dataclasses import dataclass, field
from novelagent.storage.database import get_connection


@dataclass
class Project:
    project_id: str
    name: str
    genre: str = ""
    word_count: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Session:
    session_id: str
    project_id: str
    title: str = ""
    messages_json: str = "[]"
    token_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def get_messages(self) -> list[dict]:
        return json.loads(self.messages_json)

    def set_messages(self, messages: list[dict]):
        self.messages_json = json.dumps(messages, ensure_ascii=True)


async def create_project(name: str, genre: str = "") -> Project:
    project_id = str(uuid.uuid4())
    conn = await get_connection()
    await conn.execute(
        "INSERT INTO projects (project_id, name, genre) VALUES (?, ?, ?)",
        (project_id, name, genre)
    )
    await conn.commit()
    await conn.close()
    return Project(project_id=project_id, name=name, genre=genre)


async def list_projects() -> list[Project]:
    conn = await get_connection()
    cursor = await conn.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    )
    rows = await cursor.fetchall()
    await conn.close()
    return [Project(**dict(row)) for row in rows]


async def update_word_count(project_id: str, word_count: int):
    conn = await get_connection()
    await conn.execute(
        "UPDATE projects SET word_count = ?, updated_at = datetime('now') WHERE project_id = ?",
        (word_count, project_id)
    )
    await conn.commit()
    await conn.close()


async def create_session(project_id: str) -> Session:
    session_id = str(uuid.uuid4())
    conn = await get_connection()
    await conn.execute(
        "INSERT INTO sessions (session_id, project_id) VALUES (?, ?)",
        (session_id, project_id)
    )
    await conn.commit()
    await conn.close()
    return Session(session_id=session_id, project_id=project_id)


async def load_session(session_id: str) -> Session | None:
    conn = await get_connection()
    cursor = await conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    await conn.close()
    if row is None:
        return None
    return Session(**dict(row))


async def save_messages(session_id: str, messages: list[dict], token_count: int = 0, llm_client=None):
    messages_json = json.dumps(messages, ensure_ascii=True)
    conn = await get_connection()
    cursor = await conn.execute("SELECT title FROM sessions WHERE session_id = ?", (session_id,))
    row = await cursor.fetchone()
    title = row[0] if row else ''
    if not title:
        title = await _generate_title(llm_client, messages)
    await conn.execute(
        "UPDATE sessions SET messages_json = ?, token_count = ?, title = ?, updated_at = datetime('now') WHERE session_id = ?",
        (messages_json, token_count, title or '', session_id)
    )
    await conn.commit()
    await conn.close()


async def _generate_title(llm_client, messages: list[dict]) -> str:
    """用轻量LLM根据用户消息生成10字以内的会话标题"""
    if llm_client is None:
        # 回退：取第一条用户消息前30字
        for m in messages:
            if m.get('role') == 'user' and m.get('content'):
                return m['content'].strip()[:30]
        return ''
    try:
        user_msgs = [m.get('content', '') for m in messages if m.get('role') == 'user']
        if not user_msgs:
            return ''
        recent = '\n'.join(msg[:200] for msg in user_msgs[-3:])
        prompt = f'根据以下用户消息，生成一个10字以内的简洁会话标题，只返回标题文本（不含引号）。\n\n{recent}'
        response_text = ''
        async for chunk in llm_client.chat(
            position='auto_memory',
            messages=[{'role': 'user', 'content': prompt}],
            stream=False,
        ):
            if chunk.type == 'text_delta':
                response_text += chunk.content
        title = response_text.strip().strip('"''「」『』').strip()[:30]
        return title
    except Exception:
        return ''


async def list_sessions(project_id: str) -> list[Session]:
    conn = await get_connection()
    cursor = await conn.execute(
        "SELECT * FROM sessions WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,)
    )
    rows = await cursor.fetchall()
    await conn.close()
    return [Session(**dict(row)) for row in rows]
