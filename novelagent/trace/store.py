"""SQLite persistence for immutable traces, atomic memories and reviewed patterns."""

import json
import hashlib
import uuid
from datetime import datetime, timezone

from novelagent.storage.database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _effective_importance(importance: float, last_reinforced_at: str) -> float:
    """Apply a lazy 90-day half-life without a background write job."""
    try:
        last = datetime.fromisoformat(last_reinforced_at.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() / 86400)
    except (TypeError, ValueError):
        return max(0.0, importance)
    return max(0.0, importance * (0.5 ** (days / 90)))


def _hydrate_memory(row: dict) -> dict:
    row["source_event_ids"] = json.loads(row.pop("source_event_ids_json") or "[]")
    row["target_agents"] = json.loads(row.pop("target_agents_json") or "[]")
    row["importance"] = round(_effective_importance(float(row.get("importance") or 0), row.get("last_reinforced_at", "")), 2)
    return row


def _extract_historical_turns(messages: list[dict]) -> tuple[list[dict], int]:
    """Recover user-to-final-assistant turns while retaining raw ranges for replay."""
    turns: list[dict] = []
    pending_users: list[tuple[int, dict]] = []
    segment_start: int | None = None
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            if segment_start is None:
                segment_start = index
            pending_users.append((index, message))
            continue
        is_final = role == "assistant" and not message.get("tool_calls") and bool(str(message.get("content") or "").strip())
        if not is_final or not pending_users:
            continue
        user_content = "\n\n".join(str(item.get("content") or "") for _, item in pending_users).strip()
        raw_messages = [item for item in messages[(segment_start or pending_users[0][0]) - 1:index] if isinstance(item, dict)]
        turns.append({
            "turn_no": len(turns) + 1,
            "message_start": segment_start or pending_users[0][0],
            "message_end": index,
            "user_content": user_content,
            "assistant_content": str(message.get("content") or "").strip(),
            "tool_activity": _summarize_tool_activity(raw_messages),
            "raw_messages": raw_messages,
        })
        pending_users = []
        segment_start = None
    # Consecutive user messages are one reconstructed request, so an unfinished
    # tail counts once even when it contains follow-up fragments.
    return turns, 1 if pending_users else 0


def _summarize_tool_activity(messages: list[dict]) -> list[dict]:
    calls: list[str] = []
    result_chars = 0
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    calls.append(str(call.get("tool") or function.get("name") or "unknown"))
        elif message.get("role") == "tool_result":
            result_chars += len(str(message.get("content") or ""))
    return [{"tool_names": calls, "result_characters": result_chars}] if calls or result_chars else []


async def _append_event_in_transaction(conn, trace_id: str, sequence: int, event_type: str, actor: str, payload: dict) -> None:
    await conn.execute(
        """INSERT INTO trace_events
           (event_id, trace_id, sequence_no, event_type, actor, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (f"evt_{uuid.uuid4().hex}", trace_id, sequence, event_type, actor,
         json.dumps(payload, ensure_ascii=False, default=str), _now()),
    )


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

    async def import_historical_session(self, session_id: str, project_id: str, messages: list[dict]) -> dict:
        """Create immutable evidence-only traces from a legacy ReAct transcript."""
        source_hash = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        turns, incomplete_count = _extract_historical_turns(messages)
        conn = await get_connection()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT imported_trace_count, skipped_incomplete_count FROM historical_trace_import_state WHERE session_id = ?",
                (session_id,),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await conn.rollback()
                return {
                    "status": "already_imported", "trace_count": int(existing[0]),
                    "candidate_turn_count": len(turns), "skipped_incomplete_count": int(existing[1]),
                }

            imported_trace_ids: list[str] = []
            for offset in range(0, len(turns), 5):
                batch = turns[offset:offset + 5]
                trace_id = f"tr_{uuid.uuid4().hex}"
                imported_trace_ids.append(trace_id)
                await conn.execute(
                    """INSERT INTO traces
                       (trace_id, session_id, project_id, user_message, status, final_answer, source, started_at, finished_at)
                       VALUES (?, ?, ?, ?, 'completed', ?, 'historical', ?, ?)""",
                    (trace_id, session_id, project_id, batch[-1]["user_content"], batch[-1]["assistant_content"], _now(), _now()),
                )
                sequence = 0
                for turn in batch:
                    sequence += 1
                    await _append_event_in_transaction(conn, trace_id, sequence, "user_message", "user", {
                        "source": "historical", "turn_no": turn["turn_no"],
                        "message_start": turn["message_start"], "message_end": turn["message_end"],
                        "content": turn["user_content"],
                    })
                    if turn["tool_activity"]:
                        sequence += 1
                        await _append_event_in_transaction(conn, trace_id, sequence, "historical_tool_activity", "system", {
                            "source": "historical", "turn_no": turn["turn_no"], "tools": turn["tool_activity"],
                        })
                    sequence += 1
                    await _append_event_in_transaction(conn, trace_id, sequence, "assistant_turn", "main_agent", {
                        "source": "historical", "turn_no": turn["turn_no"], "content": turn["assistant_content"],
                    })
                snapshot_messages = [message for turn in batch for message in turn["raw_messages"]]
                await conn.execute(
                    "INSERT INTO trace_snapshots (trace_id, start_turn_no, end_turn_no, messages_json) VALUES (?, ?, ?, ?)",
                    (trace_id, batch[0]["turn_no"], batch[-1]["turn_no"], json.dumps(snapshot_messages, ensure_ascii=False)),
                )

            await conn.execute(
                """INSERT INTO historical_trace_import_state
                   (session_id, source_message_count, source_hash, imported_trace_count, skipped_incomplete_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, len(messages), source_hash, len(imported_trace_ids), incomplete_count),
            )
            await conn.commit()
            return {
                "status": "imported", "trace_count": len(imported_trace_ids),
                "candidate_turn_count": len(turns), "skipped_incomplete_count": incomplete_count,
                "trace_ids": imported_trace_ids,
            }
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def append_session_turn(self, session_id: str, user_content: str, assistant_content: str,
                                  events: list[dict] | None = None) -> int:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(turn_no), 0) FROM session_trace_turns WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        turn_no = int(row[0]) + 1
        await conn.execute(
            """INSERT INTO session_trace_turns (session_id, turn_no, user_content, assistant_content, events_json)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, turn_no, user_content, assistant_content, json.dumps(events or [], ensure_ascii=False)),
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
            "SELECT turn_no, user_content, assistant_content, events_json FROM session_trace_turns WHERE session_id = ? AND turn_no >= ? ORDER BY turn_no",
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
            try:
                deferred_events = json.loads(turn.get("events_json") or "[]")
            except json.JSONDecodeError:
                deferred_events = []
            has_assistant_turn = False
            for event in deferred_events if isinstance(deferred_events, list) else []:
                if not isinstance(event, dict) or not event.get("event_type") or not event.get("actor"):
                    continue
                sequence_no += 1
                has_assistant_turn |= event["event_type"] == "assistant_turn"
                payload = dict(event.get("payload") or {})
                payload["turn_no"] = turn["turn_no"]
                await self.append_event(trace_id, sequence_no, str(event["event_type"]), str(event["actor"]), payload,
                                        duration_ms=event.get("duration_ms"))
            if not has_assistant_turn:
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
        operation_kind = await self.infer_operation_kind(trace_id)
        return {"trace_id": trace_id, "start_turn_no": start_turn_no, "end_turn_no": end_turn_no,
                "operation_kind": operation_kind, "messages": messages}

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

    async def infer_operation_kind(self, trace_id: str) -> str:
        """Mark low-value operational traces before any LLM analysis is scheduled."""
        events = await self.list_events(trace_id, limit=500)
        event_types = {event["event_type"] for event in events}
        has_assistant_text = any(
            event["event_type"] == "assistant_turn" and str(event.get("payload", {}).get("content", "")).strip()
            for event in events
        )
        has_tool_activity = bool(event_types & {"tool_call", "tool_result", "historical_tool_activity"})
        if has_tool_activity and not has_assistant_text:
            kind = "tool_only"
        elif event_types and event_types <= {"context_built", "context_compression", "llm_request", "error"}:
            kind = "system"
        else:
            kind = "conversation"
        analysis_status = "skipped" if kind in {"tool_only", "system"} else "pending"
        conn = await get_connection()
        await conn.execute(
            "UPDATE traces SET operation_kind = ?, analysis_status = ? WHERE trace_id = ?",
            (kind, analysis_status, trace_id),
        )
        await conn.commit()
        await conn.close()
        return kind

    async def set_trace_analysis_status(self, trace_id: str, status: str) -> None:
        conn = await get_connection()
        await conn.execute("UPDATE traces SET analysis_status = ? WHERE trace_id = ?", (status, trace_id))
        await conn.commit()
        await conn.close()

    async def set_trace_operation(self, trace_id: str, operation_kind: str, analysis_status: str) -> None:
        conn = await get_connection()
        await conn.execute(
            "UPDATE traces SET operation_kind = ?, analysis_status = ? WHERE trace_id = ?",
            (operation_kind, analysis_status, trace_id),
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

    async def list_project_traces(self, project_id: str, limit: int = 100) -> list[dict]:
        """List evidence records without loading their full event payloads."""
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT traces.*, trace_classifications.trace_id AS classified_trace_id,
                      trace_classifications.has_error, trace_classifications.has_correction,
                      trace_classifications.has_confirmation, trace_classifications.has_feedback,
                      COUNT(trace_events.event_id) AS event_count
               FROM traces
               LEFT JOIN trace_classifications ON trace_classifications.trace_id = traces.trace_id
               LEFT JOIN trace_events ON trace_events.trace_id = traces.trace_id
               WHERE traces.project_id = ?
               GROUP BY traces.trace_id
               ORDER BY traces.started_at DESC LIMIT ?""",
            (project_id, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        for row in rows:
            row["is_classified"] = bool(row.pop("classified_trace_id", None))
            for key in ("has_error", "has_correction", "has_confirmation", "has_feedback"):
                row[key] = bool(row.get(key))
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
                            subtype: str, claim: str, scope: str, confidence: float, *,
                            importance: float = 65, support_count: int = 1,
                            target_agents: list[str] | None = None, when_text: str = "", then_text: str = "") -> str:
        memory_id = f"mem_{uuid.uuid4().hex}"
        now = _now()
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_memories
               (memory_id, project_id, trace_id, source_event_ids_json, kind, subtype, claim, scope, confidence,
                status, importance, support_count, last_reinforced_at, target_agents_json, when_text, then_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'memory', ?, ?, ?, ?, ?, ?)""",
            (memory_id, project_id, trace_id, json.dumps(source_event_ids, ensure_ascii=False), kind, subtype,
             claim, scope, confidence, importance, support_count, now,
             json.dumps(target_agents or [], ensure_ascii=False), when_text, then_text),
        )
        await conn.commit()
        await conn.close()
        return memory_id

    async def set_memory_file_path(self, memory_id: str, file_path: str) -> None:
        conn = await get_connection()
        await conn.execute("UPDATE trace_memories SET file_path = ? WHERE memory_id = ?", (file_path, memory_id))
        await conn.commit()
        await conn.close()

    async def record_memory_evidence(self, project_id: str, trace_id: str, source_event_ids: list[str], kind: str,
                                     subtype: str, claim: str, scope: str, signal: str, confidence: float) -> str:
        evidence_id = f"evm_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO trace_memory_evidence
               (evidence_id, trace_id, project_id, source_event_ids_json, kind, subtype, claim, scope, signal, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (evidence_id, trace_id, project_id, json.dumps(source_event_ids, ensure_ascii=False), kind, subtype,
             claim, scope, signal, confidence),
        )
        await conn.commit()
        await conn.close()
        return evidence_id

    async def list_pending_evidence(self, project_id: str, limit: int = 80) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT * FROM trace_memory_evidence WHERE project_id = ? AND status = 'pending'
               ORDER BY created_at DESC LIMIT ?""", (project_id, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        for row in rows:
            row["source_event_ids"] = json.loads(row.pop("source_event_ids_json") or "[]")
        return rows

    async def attach_evidence_to_memory(self, evidence_ids: list[str], memory_id: str) -> None:
        if not evidence_ids:
            return
        conn = await get_connection()
        placeholders = ",".join("?" for _ in evidence_ids)
        await conn.execute(
            f"UPDATE trace_memory_evidence SET status = 'materialized', memory_id = ? WHERE evidence_id IN ({placeholders})",
            (memory_id, *evidence_ids),
        )
        await conn.commit()
        await conn.close()

    async def reinforce_memory(self, memory_id: str, evidence_ids: list[str], weight: float,
                               target_agents: list[str] | None = None, when_text: str = "", then_text: str = "") -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM trace_memories WHERE memory_id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            await conn.close()
            return None
        memory = dict(row)
        importance = min(100.0, _effective_importance(float(memory.get("importance") or 0), memory.get("last_reinforced_at", "")) + weight)
        support_count = int(memory.get("support_count") or 0) + 1
        status = "rule" if importance >= 80 and support_count >= 3 else "memory"
        existing_agents = json.loads(memory.get("target_agents_json") or "[]")
        agents = target_agents or existing_agents
        await conn.execute(
            """UPDATE trace_memories SET importance = ?, support_count = ?, last_reinforced_at = ?, status = ?,
               target_agents_json = ?, when_text = ?, then_text = ? WHERE memory_id = ?""",
            (importance, support_count, _now(), status, json.dumps(agents, ensure_ascii=False),
             when_text or memory.get("when_text", ""), then_text or memory.get("then_text", ""), memory_id),
        )
        await conn.commit()
        await conn.close()
        await self.attach_evidence_to_memory(evidence_ids, memory_id)
        return await self.get_memory(memory["project_id"], memory_id)

    async def contradict_memory(self, memory_id: str, evidence_ids: list[str]) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM trace_memories WHERE memory_id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            await conn.close()
            return None
        memory = dict(row)
        importance = max(0.0, _effective_importance(float(memory.get("importance") or 0), memory.get("last_reinforced_at", "")) - 50)
        status = "memory" if memory.get("status") == "rule" else "disputed"
        await conn.execute(
            """UPDATE trace_memories SET importance = ?, contradiction_count = contradiction_count + 1,
               last_reinforced_at = ?, status = ? WHERE memory_id = ?""",
            (importance, _now(), status, memory_id),
        )
        await conn.commit()
        await conn.close()
        await self.attach_evidence_to_memory(evidence_ids, memory_id)
        return await self.get_memory(memory["project_id"], memory_id)

    async def downgrade_memory(self, project_id: str, memory_id: str) -> dict | None:
        """Manually lower one lifecycle level without discarding its trace evidence."""
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_memories WHERE project_id = ? AND memory_id = ?", (project_id, memory_id)
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.close()
            return None
        memory = dict(row)
        status = memory.get("status", "memory")
        if status == "rule":
            importance = min(79.0, max(0.0, float(memory.get("importance") or 0) - 30.0))
            await conn.execute(
                "UPDATE trace_memories SET status = 'memory', importance = ? WHERE memory_id = ?",
                (importance, memory_id),
            )
            await conn.commit()
            await conn.close()
            result = await self.get_memory(project_id, memory_id)
            return {"action": "rule_to_memory", "memory": result}

        if status not in {"memory", "disputed"}:
            await conn.close()
            return None

        evidence_cursor = await conn.execute(
            "SELECT evidence_id FROM trace_memory_evidence WHERE memory_id = ?", (memory_id,)
        )
        evidence_ids = [row["evidence_id"] for row in await evidence_cursor.fetchall()]
        if not evidence_ids:
            evidence_id = f"evm_{uuid.uuid4().hex}"
            await conn.execute(
                """INSERT INTO trace_memory_evidence
                   (evidence_id, trace_id, project_id, source_event_ids_json, kind, subtype, claim, scope, signal, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'weak', ?)""",
                (
                    evidence_id, memory["trace_id"], project_id, memory["source_event_ids_json"], memory["kind"],
                    memory["subtype"], memory["claim"], memory["scope"], memory["confidence"],
                ),
            )
            evidence_ids = [evidence_id]
        placeholders = ",".join("?" for _ in evidence_ids)
        await conn.execute(
            f"UPDATE trace_memory_evidence SET status = 'pending', memory_id = '' WHERE evidence_id IN ({placeholders})",
            evidence_ids,
        )
        await conn.execute(
            "UPDATE trace_memories SET status = 'trace', importance = ? WHERE memory_id = ?",
            (max(0.0, float(memory.get("importance") or 0) - 30.0), memory_id),
        )
        await conn.commit()
        await conn.close()
        return {"action": "memory_to_trace", "trace_id": memory["trace_id"], "evidence_ids": evidence_ids}

    async def list_memories(self, project_id: str, limit: int = 100, include_rules: bool = False,
                            include_trace: bool = False) -> list[dict]:
        conn = await get_connection()
        statuses = "'memory', 'rule', 'disputed', 'trace'" if include_trace else (
            "'memory', 'rule', 'disputed'" if include_rules else "'memory', 'disputed'"
        )
        where = f"project_id = ? AND status IN ({statuses})"
        cursor = await conn.execute(
            f"SELECT * FROM trace_memories WHERE {where} ORDER BY last_reinforced_at DESC, created_at DESC LIMIT ?", (project_id, limit)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return [_hydrate_memory(row) for row in rows]

    async def list_rules(self, project_id: str, limit: int = 100) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT * FROM trace_memories WHERE project_id = ? AND status = 'rule'
               ORDER BY importance DESC, last_reinforced_at DESC LIMIT ?""", (project_id, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        # Importance decays lazily. A stale rule must not be injected merely
        # because its stored status predates that decay.
        return [memory for memory in (_hydrate_memory(row) for row in rows) if memory["importance"] >= 80]

    async def get_memory(self, project_id: str, memory_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM trace_memories WHERE project_id = ? AND memory_id = ?", (project_id, memory_id)
        )
        row = await cursor.fetchone()
        await conn.close()
        if row is None:
            return None
        return _hydrate_memory(dict(row))

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
