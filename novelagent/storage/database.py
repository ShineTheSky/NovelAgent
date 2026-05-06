"""SQLite 连接管理 + 自动建表"""

import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "novelagent.db")


async def get_db_path() -> str:
    """获取数据库文件路径，确保目录存在"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return DB_PATH


async def init():
    """初始化数据库：创建表结构"""
    db_path = await get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                genre TEXT DEFAULT '',
                word_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                messages_json TEXT NOT NULL DEFAULT '[]',
                token_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (project_id) REFERENCES projects(project_id)
            )
        """)
        await db.commit()


async def get_connection() -> aiosqlite.Connection:
    """获取数据库连接（自动初始化）"""
    db_path = await get_db_path()
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn
