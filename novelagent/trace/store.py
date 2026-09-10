"""SQLite persistence for immutable traces, atomic memories and reviewed patterns."""

import json
import uuid
from datetime import datetime, timezone

from novelagent.storage.database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    async def create_trace(self, session_id: str, project_id: str, user_message: str) -> str:
        trace_id = f"tr_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            "INSERT INTO traces (trace_id, session_id, project_id, user_message, started_at) VALUES (?, ?, ?, ?, ?)",
            (trace_id, session_id, project_id, user_message, _now()),
        )
        await conn.commit()
        await conn.close()
        return trace_id

    async def append_event(self, trace_id: str, sequence_no: int, event_type: str, actor: str,
                           payload: dict | None = None, parent_event_id: str | None = None,
                           duration_ms: float | None = None) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_events
               (event_id, trace_id, parent_event_id, sequence_no, event_type, actor, payload_json, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, trace_id, parent_event_id, sequence_no, event_type, actor,
             json.dumps(payload or {}, ensure_ascii=False, default=str), duration_ms, _now()),
        )
        await conn.commit()
        await conn.close()
        return event_id

    async def finish_trace(self, trace_id: str, status: str, final_answer: str = "", token_count: int = 0) -> None:
        conn = await get_connection()
        await conn.execute(
            "UPDATE traces SET status = ?, final_answer = ?, token_count = ?, finished_at = ? WHERE trace_id = ?",
            (status, final_answer, token_count, _now(), trace_id),
        )
        await conn.commit()
        await conn.close()

    async def get_trace(self, trace_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
        row = await cursor.fetchone()
        await conn.close()
        return dict(row) if row else None

    async def list_session_traces(self, session_id: str, limit: int = 50) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY started_at DESC LIMIT ?", (session_id, limit)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows

    async def list_events(self, trace_id: str, limit: int = 80) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY sequence_no ASC LIMIT ?", (trace_id, limit)
        )
        rows = await cursor.fetchall()
        await conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    async def create_memory(self, project_id: str, trace_id: str, source_event_ids: list[str], kind: str,
                            subtype: str, claim: str, scope: str, confidence: float) -> str:
        memory_id = f"mem_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_memories
               (memory_id, project_id, trace_id, source_event_ids_json, kind, subtype, claim, scope, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, project_id, trace_id, json.dumps(source_event_ids, ensure_ascii=False), kind, subtype,
             claim, scope, confidence),
        )
        await conn.commit()
        await conn.close()
        return memory_id

    async def set_memory_file_path(self, memory_id: str, file_path: str) -> None:
        conn = await get_connection()
        await conn.execute("UPDATE trace_memories SET file_path = ? WHERE memory_id = ?", (file_path, memory_id))
        await conn.commit()
        await conn.close()

    async def list_memories(self, project_id: str, limit: int = 100) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_memories WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", (project_id, limit)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        for row in rows:
            row["source_event_ids"] = json.loads(row.pop("source_event_ids_json") or "[]")
        return rows

    async def get_memory(self, project_id: str, memory_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_memories WHERE project_id = ? AND memory_id = ?", (project_id, memory_id)
        )
        row = await cursor.fetchone()
        await conn.close()
        if row is None:
            return None
        result = dict(row)
        result["source_event_ids"] = json.loads(result.pop("source_event_ids_json") or "[]")
        return result

    async def list_patterns(self, project_id: str, limit: int = 50, statuses: tuple[str, ...] | None = None) -> list[dict]:
        statuses = statuses or ("tentative", "confirmed", "disputed", "ready_for_review")
        placeholders = ",".join("?" for _ in statuses)
        conn = await get_connection()
        cursor = await conn.execute(
            f"""SELECT * FROM memory_patterns WHERE project_id = ? AND status IN ({placeholders})
                ORDER BY updated_at DESC LIMIT ?""",
            (project_id, *statuses, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows

    async def create_pattern(self, project_id: str, kind: str, subtype: str, dimension: str,
                             canonical_claim: str, scope: str, confidence: float) -> str:
        pattern_id = f"pat_{uuid.uuid4().hex}"
        now = _now()
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO memory_patterns
               (pattern_id, project_id, kind, subtype, dimension, canonical_claim, scope, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pattern_id, project_id, kind, subtype, dimension, canonical_claim, scope, confidence, now, now),
        )
        await conn.commit()
        await conn.close()
        return pattern_id

    async def attach_memory(self, pattern_id: str, memory_id: str, relation: str) -> None:
        conn = await get_connection()
        await conn.execute(
            "INSERT OR IGNORE INTO pattern_memories (pattern_id, memory_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (pattern_id, memory_id, relation, _now()),
        )
        await conn.execute(
            """UPDATE memory_patterns
               SET support_count = (SELECT COUNT(*) FROM pattern_memories WHERE pattern_id = ? AND relation = 'support'),
                   contradiction_count = (SELECT COUNT(*) FROM pattern_memories WHERE pattern_id = ? AND relation = 'contradict'),
                   updated_at = ?
               WHERE pattern_id = ?""",
            (pattern_id, pattern_id, _now(), pattern_id),
        )
        await conn.commit()
        await conn.close()

    async def get_pattern(self, pattern_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM memory_patterns WHERE pattern_id = ?", (pattern_id,))
        row = await cursor.fetchone()
        await conn.close()
        return dict(row) if row else None

    async def set_pattern_review(self, pattern_id: str, status: str, confidence: float) -> None:
        conn = await get_connection()
        await conn.execute(
            "UPDATE memory_patterns SET status = ?, confidence = ?, updated_at = ? WHERE pattern_id = ?",
            (status, confidence, _now(), pattern_id),
        )
        await conn.commit()
        await conn.close()

    async def list_pattern_memories(self, pattern_id: str) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT m.*, pm.relation FROM trace_memories m
               JOIN pattern_memories pm ON pm.memory_id = m.memory_id
               WHERE pm.pattern_id = ? ORDER BY m.created_at ASC""", (pattern_id,)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows
