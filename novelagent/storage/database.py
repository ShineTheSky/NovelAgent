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
        # 仅用于恢复前端展示；不能混入 sessions.messages_json 的模型上下文。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_display_turns (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (session_id, turn_no),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        # 仅用于恢复前端展示；不能混入 sessions.messages_json 的模型上下文。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_display_turns (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (session_id, turn_no),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
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
        await _ensure_column(db, "traces", "source", "TEXT NOT NULL DEFAULT 'live'")
        await _ensure_column(db, "traces", "operation_kind", "TEXT NOT NULL DEFAULT 'conversation'")
        await _ensure_column(db, "traces", "analysis_status", "TEXT NOT NULL DEFAULT 'pending'")
        await _ensure_column(db, "traces", "turn_no", "INTEGER")
        await _ensure_column(db, "traces", "previous_trace_id", "TEXT")
        await _ensure_column(db, "traces", "next_trace_id", "TEXT")
        await _ensure_column(db, "traces", "source_agent", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "traces", "agent_position", "TEXT NOT NULL DEFAULT ''")
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
        await _ensure_column(db, "session_trace_turns", "events_json", "TEXT NOT NULL DEFAULT '[]'")
        await _ensure_column(db, "session_trace_turns", "source_trace_id", "TEXT")
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
            CREATE TABLE IF NOT EXISTS trace_analysis_windows (
                window_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                start_turn_no INTEGER NOT NULL,
                end_turn_no INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT 'interval',
                status TEXT NOT NULL DEFAULT 'pending',
                token_count INTEGER NOT NULL DEFAULT 0,
                messages_json TEXT NOT NULL DEFAULT '[]',
                normalized_trajectory_json TEXT NOT NULL DEFAULT '[]',
                trajectory_provenance_json TEXT NOT NULL DEFAULT '[]',
                trajectory_trace_id TEXT NOT NULL DEFAULT '',
                normalized_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        await _ensure_column(db, "trace_analysis_windows", "summary", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "trace_analysis_windows", "summary_trace_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "trace_analysis_windows", "summary_start_turn_no", "INTEGER")
        await _ensure_column(db, "trace_analysis_windows", "summary_end_turn_no", "INTEGER")
        await _ensure_column(db, "trace_analysis_windows", "normalized_trajectory_json", "TEXT NOT NULL DEFAULT '[]'")
        await _ensure_column(db, "trace_analysis_windows", "trajectory_provenance_json", "TEXT NOT NULL DEFAULT '[]'")
        await _ensure_column(db, "trace_analysis_windows", "trajectory_trace_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "trace_analysis_windows", "normalized_at", "TEXT")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_compression_state (
                session_id TEXT PRIMARY KEY,
                last_compressed_turn_no INTEGER NOT NULL DEFAULT 0,
                messages_json TEXT NOT NULL DEFAULT '[]',
                compressed_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_window_members (
                window_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                member_no INTEGER NOT NULL,
                PRIMARY KEY (window_id, trace_id),
                FOREIGN KEY (window_id) REFERENCES trace_analysis_windows(window_id),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_traces_session_turn ON traces(session_id, turn_no)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_window_members_trace ON trace_window_members(trace_id)")
        # Legacy Bad Case records retain their original payloads.  This table
        # stores only a separately verified link to a real request Trace.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bad_case_trace_links (
                bad_case_trace_id TEXT PRIMARY KEY,
                legacy_source_trace_id TEXT NOT NULL DEFAULT '',
                resolved_trace_id TEXT,
                status TEXT NOT NULL DEFAULT 'unmapped_legacy',
                match_method TEXT NOT NULL DEFAULT '',
                candidate_trace_ids_json TEXT NOT NULL DEFAULT '[]',
                linked_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (bad_case_trace_id) REFERENCES traces(trace_id),
                FOREIGN KEY (resolved_trace_id) REFERENCES traces(trace_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS historical_trace_import_state (
                session_id TEXT PRIMARY KEY,
                source_message_count INTEGER NOT NULL DEFAULT 0,
                source_hash TEXT NOT NULL DEFAULT '',
                imported_trace_count INTEGER NOT NULL DEFAULT 0,
                skipped_incomplete_count INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
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
        # Older Trace rows predate explicit Agent provenance. Backfill only
        # derivable values; future rows record these fields at creation time.
        await db.execute("""
            UPDATE traces SET source_agent = CASE
                WHEN operation_kind = 'agent_run' THEN COALESCE(
                    (SELECT actor FROM trace_events
                     WHERE trace_events.trace_id = traces.trace_id AND event_type = 'agent_started'
                     ORDER BY sequence_no LIMIT 1), '')
                WHEN operation_kind = 'agent_bad_case' THEN COALESCE(
                    (SELECT actor FROM trace_events
                     WHERE trace_events.trace_id = traces.trace_id AND event_type = 'error'
                     ORDER BY sequence_no LIMIT 1), '')
                WHEN operation_kind IN ('conversation', 'context_transition', 'routine', 'tool_only')
                    THEN 'main_agent'
                ELSE source_agent
            END
            WHERE source_agent = ''
        """)
        await db.execute("""
            UPDATE traces SET agent_position = CASE
                WHEN operation_kind IN ('conversation', 'context_transition', 'routine', 'tool_only')
                    THEN 'main_loop'
                ELSE agent_position
            END
            WHERE agent_position = ''
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
        await _ensure_column(db, "trace_memories", "importance", "REAL NOT NULL DEFAULT 0")
        await _ensure_column(db, "trace_memories", "support_count", "INTEGER NOT NULL DEFAULT 1")
        await _ensure_column(db, "trace_memories", "contradiction_count", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "trace_memories", "last_reinforced_at", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "trace_memories", "target_agents_json", "TEXT NOT NULL DEFAULT '[]'")
        await _ensure_column(db, "trace_memories", "when_text", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "trace_memories", "then_text", "TEXT NOT NULL DEFAULT ''")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trace_memory_evidence (
                evidence_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                kind TEXT NOT NULL,
                subtype TEXT NOT NULL DEFAULT '',
                claim TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'project',
                signal TEXT NOT NULL DEFAULT 'weak',
                confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                memory_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id),
                FOREIGN KEY (memory_id) REFERENCES trace_memories(memory_id)
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_memory_evidence_project ON trace_memory_evidence(project_id, status, kind)")
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
                chunk_id TEXT PRIMARY KEY,
                dimensions INTEGER NOT NULL,
                vector_blob BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (chunk_id) REFERENCES rag_chunks(chunk_id)
            )
        """)
        await _migrate_rag_to_global(db)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_documents_created ON rag_documents(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_document ON rag_chunks(document_id)")
        await db.commit()


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
