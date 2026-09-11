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
        # Trace 是每次 Agent 执行的不可变事实记录；sessions.messages_json
        # 仍然仅用于恢复会话，不承担分析与审计职责。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                user_message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                final_answer TEXT NOT NULL DEFAULT '',
                token_count INTEGER DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_trace_turns (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                user_content TEXT NOT NULL DEFAULT '',
                assistant_content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (session_id, turn_no),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_trace_state (
                session_id TEXT PRIMARY KEY,
                last_captured_turn INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_snapshots (
                trace_id TEXT PRIMARY KEY,
                start_turn_no INTEGER NOT NULL,
                end_turn_no INTEGER NOT NULL,
                messages_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_events (
                event_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_event_id TEXT,
                sequence_no INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                duration_ms REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id),
                UNIQUE(trace_id, sequence_no)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_memories (
                memory_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                kind TEXT NOT NULL,
                subtype TEXT NOT NULL DEFAULT '',
                claim TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'project',
                confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                file_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_patterns (
                pattern_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                subtype TEXT NOT NULL DEFAULT '',
                dimension TEXT NOT NULL DEFAULT '',
                canonical_claim TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'project',
                status TEXT NOT NULL DEFAULT 'tentative',
                confidence REAL NOT NULL DEFAULT 0,
                support_count INTEGER NOT NULL DEFAULT 0,
                contradiction_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pattern_memories (
                pattern_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                relation TEXT NOT NULL DEFAULT 'support',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (pattern_id, memory_id),
                FOREIGN KEY (pattern_id) REFERENCES memory_patterns(pattern_id),
                FOREIGN KEY (memory_id) REFERENCES trace_memories(memory_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_pattern_summaries (
                pattern_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (pattern_id) REFERENCES memory_patterns(pattern_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, sequence_no)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_classifications (
                trace_id TEXT PRIMARY KEY,
                has_error INTEGER NOT NULL DEFAULT 0,
                has_correction INTEGER NOT NULL DEFAULT 0,
                has_confirmation INTEGER NOT NULL DEFAULT 0,
                has_feedback INTEGER NOT NULL DEFAULT 0,
                items_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_memories_project ON trace_memories(project_id, kind, subtype)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_patterns_project ON memory_patterns(project_id, kind, subtype, status)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rag_documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                encoding TEXT NOT NULL DEFAULT 'utf-8',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES rag_documents(document_id)
            )
        """)
        await _migrate_rag_to_global(db)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_documents_created ON rag_documents(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_document ON rag_chunks(document_id)")
        await db.commit()


async def _migrate_rag_to_global(db: aiosqlite.Connection) -> None:
    """Remove the old per-project RAG scope while preserving imported documents."""
    cursor = await db.execute("PRAGMA table_info(rag_documents)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "project_id" not in columns:
        return

    await db.execute("DROP INDEX IF EXISTS idx_rag_documents_project")
    await db.execute("DROP INDEX IF EXISTS idx_rag_chunks_project")
    await db.execute("""
        CREATE TABLE rag_documents_global (
            document_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_name TEXT DEFAULT '',
            encoding TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute("""
        INSERT INTO rag_documents_global (document_id, title, source_name, encoding, created_at)
        SELECT document_id, title, source_name, 'unknown', created_at FROM rag_documents
    """)
    await db.execute("""
        CREATE TABLE rag_chunks_global (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            FOREIGN KEY (document_id) REFERENCES rag_documents_global(document_id)
        )
    """)
    await db.execute("""
        INSERT INTO rag_chunks_global (chunk_id, document_id, chunk_index, content)
        SELECT chunk_id, document_id, chunk_index, content FROM rag_chunks
    """)
    await db.execute("DROP TABLE rag_chunks")
    await db.execute("DROP TABLE rag_documents")
    await db.execute("ALTER TABLE rag_documents_global RENAME TO rag_documents")
    await db.execute("ALTER TABLE rag_chunks_global RENAME TO rag_chunks")


async def get_connection() -> aiosqlite.Connection:
    """获取数据库连接（自动初始化）"""
    db_path = await get_db_path()
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn
