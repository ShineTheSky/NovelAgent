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

    async def append_session_turn(self, session_id: str, user_content: str, assistant_content: str) -> int:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(turn_no), 0) FROM session_trace_turns WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        turn_no = int(row[0]) + 1
        await conn.execute(
            "INSERT INTO session_trace_turns (session_id, turn_no, user_content, assistant_content) VALUES (?, ?, ?, ?)",
            (session_id, turn_no, user_content, assistant_content),
        )
        await conn.commit()
        await conn.close()
        return turn_no

    async def capture_pending_trace(
        self, session_id: str, project_id: str, messages: list[dict], token_count: int = 0
    ) -> dict | None:
        conn = await get_connection()
        await conn.execute(
            "INSERT OR IGNORE INTO session_trace_state (session_id) VALUES (?)", (session_id,)
        )
        cursor = await conn.execute(
            "SELECT last_captured_turn FROM session_trace_state WHERE session_id = ?", (session_id,)
        )
        state = await cursor.fetchone()
        start_turn_no = int(state[0]) + 1
        cursor = await conn.execute(
            "SELECT turn_no, user_content, assistant_content FROM session_trace_turns WHERE session_id = ? AND turn_no >= ? ORDER BY turn_no",
            (session_id, start_turn_no),
        )
        turns = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        if not turns:
            return None

        end_turn_no = turns[-1]["turn_no"]
        trace_id = await self.create_trace(session_id, project_id, turns[-1]["user_content"])
        sequence_no = 0
        for turn in turns:
            sequence_no += 1
            await self.append_event(trace_id, sequence_no, "user_message", "user", {
                "turn_no": turn["turn_no"], "content": turn["user_content"],
            })
            sequence_no += 1
            await self.append_event(trace_id, sequence_no, "assistant_turn", "main_agent", {
                "turn_no": turn["turn_no"], "content": turn["assistant_content"],
            })

        conn = await get_connection()
        await conn.execute(
            "INSERT INTO trace_snapshots (trace_id, start_turn_no, end_turn_no, messages_json) VALUES (?, ?, ?, ?)",
            (trace_id, start_turn_no, end_turn_no, json.dumps(messages, ensure_ascii=False)),
        )
        await conn.execute(
            "UPDATE session_trace_state SET last_captured_turn = ? WHERE session_id = ?",
            (end_turn_no, session_id),
        )
        await conn.commit()
        await conn.close()
        await self.finish_trace(trace_id, "completed", turns[-1]["assistant_content"], token_count)
        return {"trace_id": trace_id, "start_turn_no": start_turn_no, "end_turn_no": end_turn_no, "messages": messages}

    async def pending_trace_turn_count(self, session_id: str) -> int:
        conn = await get_connection()
        await conn.execute(
            "INSERT OR IGNORE INTO session_trace_state (session_id) VALUES (?)", (session_id,)
        )
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM session_trace_turns
               WHERE session_id = ? AND turn_no > (
                   SELECT last_captured_turn FROM session_trace_state WHERE session_id = ?
               )""",
            (session_id, session_id),
        )
        row = await cursor.fetchone()
        await conn.commit()
        await conn.close()
        return int(row[0]) if row else 0

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

    async def get_project_trace(self, project_id: str, trace_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM traces WHERE project_id = ? AND trace_id = ?", (project_id, trace_id)
        )
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

    async def count_completed_session_traces(self, session_id: str) -> int:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM traces WHERE session_id = ? AND status = 'completed'", (session_id,)
        )
        row = await cursor.fetchone()
        await conn.close()
        return int(row[0]) if row else 0

    async def list_events(self, trace_id: str, limit: int = 1000, offset: int = 0) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY sequence_no ASC LIMIT ? OFFSET ?", (trace_id, limit, offset)
        )
        rows = await cursor.fetchall()
        await conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    async def get_events_by_ids(self, trace_id: str, event_ids: list[str]) -> list[dict]:
        wanted = list(dict.fromkeys(event_id for event_id in event_ids if event_id))
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        conn = await get_connection()
        cursor = await conn.execute(
            f"SELECT * FROM trace_events WHERE trace_id = ? AND event_id IN ({placeholders}) ORDER BY sequence_no ASC",
            (trace_id, *wanted),
        )
        rows = await cursor.fetchall()
        await conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    async def get_trace_event_window(self, trace_id: str, event_ids: list[str], before: int = 2, after: int = 2) -> list[dict]:
        anchors = await self.get_events_by_ids(trace_id, event_ids)
        if not anchors:
            return []
        low = max(1, min(event["sequence_no"] for event in anchors) - max(0, before))
        high = max(event["sequence_no"] for event in anchors) + max(0, after)
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT * FROM trace_events WHERE trace_id = ? AND sequence_no BETWEEN ? AND ?
               ORDER BY sequence_no ASC""",
            (trace_id, low, high),
        )
        rows = await cursor.fetchall()
        await conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    async def save_trace_classification(self, trace_id: str, items: list[dict]) -> None:
        categories = {item["type"] for item in items}
        now = _now()
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_classifications
               (trace_id, has_error, has_correction, has_confirmation, has_feedback, items_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trace_id) DO UPDATE SET
                   has_error = excluded.has_error,
                   has_correction = excluded.has_correction,
                   has_confirmation = excluded.has_confirmation,
                   has_feedback = excluded.has_feedback,
                   items_json = excluded.items_json,
                   updated_at = excluded.updated_at""",
            (
                trace_id,
                int("error" in categories),
                int("correction" in categories),
                int("confirmation" in categories),
                int("feedback" in categories),
                json.dumps(items, ensure_ascii=False),
                now,
                now,
            ),
        )
        await conn.commit()
        await conn.close()

    async def get_trace_classification(self, trace_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM trace_classifications WHERE trace_id = ?", (trace_id,))
        row = await cursor.fetchone()
        await conn.close()
        if row is None:
            return None
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json") or "[]")
        for key in ("has_error", "has_correction", "has_confirmation", "has_feedback"):
            result[key] = bool(result[key])
        return result

    async def create_memory(self, project_id: str, trace_id: str, source_event_ids: list[str], kind: str,
                            subtype: str, claim: str, scope: str, confidence: float, status: str = "candidate") -> str:
        memory_id = f"mem_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_memories
               (memory_id, project_id, trace_id, source_event_ids_json, kind, subtype, claim, scope, confidence, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, project_id, trace_id, json.dumps(source_event_ids, ensure_ascii=False), kind, subtype,
             claim, scope, confidence, status),
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

    async def get_pattern_summary(self, pattern_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM memory_pattern_summaries WHERE pattern_id = ?", (pattern_id,)
        )
        row = await cursor.fetchone()
        await conn.close()
        if row is None:
            return None
        result = dict(row)
        result["source_memory_ids"] = json.loads(result.pop("source_memory_ids_json") or "[]")
        return result

    async def save_pattern_summary(self, pattern_id: str, summary: str, source_memory_ids: list[str]) -> None:
        now = _now()
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO memory_pattern_summaries
               (pattern_id, summary, source_memory_ids_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pattern_id) DO UPDATE SET
                   summary = excluded.summary,
                   source_memory_ids_json = excluded.source_memory_ids_json,
                   updated_at = excluded.updated_at""",
            (pattern_id, summary, json.dumps(source_memory_ids, ensure_ascii=False), now, now),
        )
        await conn.commit()
        await conn.close()

    async def update_pattern_claim(self, pattern_id: str, claim: str) -> None:
        conn = await get_connection()
        await conn.execute(
            "UPDATE memory_patterns SET canonical_claim = ?, updated_at = ? WHERE pattern_id = ?",
            (claim, _now(), pattern_id),
        )
        await conn.commit()
        await conn.close()

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
