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
    async def create_trace(self, session_id: str, project_id: str, user_message: str,
                           source_agent: str = "", agent_position: str = "") -> str:
        trace_id = f"tr_{uuid.uuid4().hex}"
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO traces
               (trace_id, session_id, project_id, user_message, source_agent, agent_position, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, session_id, project_id, user_message, source_agent, agent_position, _now()),
        )
        await conn.commit()
        await conn.close()
        return trace_id

    async def link_trace_successor(self, previous_trace_id: str, next_trace_id: str) -> None:
        """Link two immutable Trace segments created by one context transition."""
        conn = await get_connection()
        await conn.execute(
            "UPDATE traces SET next_trace_id = ? WHERE trace_id = ?",
            (next_trace_id, previous_trace_id),
        )
        await conn.execute(
            "UPDATE traces SET previous_trace_id = ? WHERE trace_id = ?",
            (previous_trace_id, next_trace_id),
        )
        await conn.commit()
        await conn.close()

    async def latest_session_turn_trace(self, session_id: str) -> str:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT source_trace_id FROM session_trace_turns
               WHERE session_id = ? AND source_trace_id IS NOT NULL
               ORDER BY turn_no DESC LIMIT 1""",
            (session_id,),
        )
        row = await cursor.fetchone()
        await conn.close()
        return str(row[0]) if row and row[0] else ""

    async def replace_latest_turn_trace(self, session_id: str, previous_trace_id: str,
                                        next_trace_id: str) -> bool:
        """Attach an out-of-band context transition to the latest Session turn."""
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT turn_no, source_trace_id FROM session_trace_turns
               WHERE session_id = ? ORDER BY turn_no DESC LIMIT 1""",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None or str(row[1] or "") != previous_trace_id:
            await conn.close()
            return False
        turn_no = int(row[0])
        await conn.execute(
            "UPDATE session_trace_turns SET source_trace_id = ? WHERE session_id = ? AND turn_no = ?",
            (next_trace_id, session_id, turn_no),
        )
        await conn.execute(
            "UPDATE traces SET turn_no = ? WHERE trace_id = ?",
            (turn_no, next_trace_id),
        )
        await conn.commit()
        await conn.close()
        return True

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
            previous_trace_id: str | None = None
            for offset in range(0, len(turns), 5):
                batch = turns[offset:offset + 5]
                trace_id = f"tr_{uuid.uuid4().hex}"
                imported_trace_ids.append(trace_id)
                await conn.execute(
                    """INSERT INTO traces
                       (trace_id, session_id, project_id, user_message, status, final_answer, source, turn_no,
                        previous_trace_id, source_agent, agent_position, started_at, finished_at)
                       VALUES (?, ?, ?, ?, 'completed', ?, 'historical', ?, ?, 'main_agent', 'main_loop', ?, ?)""",
                    (trace_id, session_id, project_id, batch[-1]["user_content"], batch[-1]["assistant_content"],
                     batch[-1]["turn_no"], previous_trace_id, _now(), _now()),
                )
                if previous_trace_id:
                    await conn.execute("UPDATE traces SET next_trace_id = ? WHERE trace_id = ?", (trace_id, previous_trace_id))
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
                previous_trace_id = trace_id

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
                                  events: list[dict] | None = None, source_trace_id: str = "") -> int:
        from novelagent.trace.stream_compaction import compact_trace_snapshot_events

        events = compact_trace_snapshot_events(events or [])
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(turn_no), 0) FROM session_trace_turns WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        turn_no = int(row[0]) + 1
        await conn.execute(
            """INSERT INTO session_trace_turns
               (session_id, turn_no, user_content, assistant_content, events_json, source_trace_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, turn_no, user_content, assistant_content, json.dumps(events, ensure_ascii=False), source_trace_id or None),
        )
        if source_trace_id:
            # A request may contain multiple immutable Trace segments when its
            # context is compressed.  Preserve that A -> B chain and assign the
            # same Session turn number to every segment.
            segment_ids = [source_trace_id]
            segment_head = source_trace_id
            for _ in range(32):
                cursor = await conn.execute(
                    "SELECT previous_trace_id FROM traces WHERE trace_id = ? AND session_id = ?",
                    (segment_head, session_id),
                )
                current_row = await cursor.fetchone()
                previous_segment_id = str(current_row[0]) if current_row and current_row[0] else ""
                if not previous_segment_id:
                    break
                cursor = await conn.execute(
                    "SELECT turn_no FROM traces WHERE trace_id = ? AND session_id = ?",
                    (previous_segment_id, session_id),
                )
                previous_segment = await cursor.fetchone()
                if previous_segment is None or previous_segment[0] is not None:
                    break
                segment_ids.append(previous_segment_id)
                segment_head = previous_segment_id
            previous = await conn.execute(
                """SELECT source_trace_id FROM session_trace_turns
                   WHERE session_id = ? AND turn_no < ? AND source_trace_id IS NOT NULL
                   ORDER BY turn_no DESC LIMIT 1""",
                (session_id, turn_no),
            )
            previous_row = await previous.fetchone()
            previous_trace_id = str(previous_row[0]) if previous_row and previous_row[0] else None
            placeholders = ",".join("?" for _ in segment_ids)
            await conn.execute(
                f"UPDATE traces SET turn_no = ? WHERE trace_id IN ({placeholders})",
                (turn_no, *segment_ids),
            )
            if previous_trace_id:
                await conn.execute(
                    "UPDATE traces SET previous_trace_id = ? WHERE trace_id = ? AND previous_trace_id IS NULL",
                    (previous_trace_id, segment_head),
                )
                await conn.execute(
                    "UPDATE traces SET next_trace_id = ? WHERE trace_id = ?",
                    (segment_head, previous_trace_id),
                )
        await conn.commit()
        await conn.close()
        return turn_no

    async def capture_pending_trace_window(
        self, session_id: str, project_id: str, messages: list[dict], token_count: int = 0, reason: str = "interval",
    ) -> dict | None:
        """Create an analysis window that references raw request traces without copying their events."""
        conn = await get_connection()
        await conn.execute("INSERT OR IGNORE INTO session_trace_state (session_id) VALUES (?)", (session_id,))
        cursor = await conn.execute("SELECT last_captured_turn FROM session_trace_state WHERE session_id = ?", (session_id,))
        state = await cursor.fetchone()
        start_turn_no = int(state[0]) + 1
        cursor = await conn.execute(
            """SELECT turn_no, source_trace_id FROM session_trace_turns
               WHERE session_id = ? AND turn_no >= ? ORDER BY turn_no""",
            (session_id, start_turn_no),
        )
        turns = [dict(row) for row in await cursor.fetchall()]
        source_trace_ids = []
        for turn in turns:
            source_trace_id = str(turn.get("source_trace_id") or "")
            if not source_trace_id:
                continue
            segment_ids = [source_trace_id]
            segment_id = source_trace_id
            for _ in range(32):
                cursor = await conn.execute(
                    "SELECT previous_trace_id FROM traces WHERE trace_id = ? AND session_id = ? AND turn_no = ?",
                    (segment_id, session_id, int(turn["turn_no"])),
                )
                row = await cursor.fetchone()
                previous_segment_id = str(row[0]) if row and row[0] else ""
                if not previous_segment_id:
                    break
                segment_ids.append(previous_segment_id)
                segment_id = previous_segment_id
            source_trace_ids.extend(reversed(segment_ids))
        if not turns or not source_trace_ids:
            await conn.close()
            return None
        end_turn_no = int(turns[-1]["turn_no"])
        window_id = f"tw_{uuid.uuid4().hex}"
        await conn.execute(
            """INSERT INTO trace_analysis_windows
               (window_id, session_id, project_id, start_turn_no, end_turn_no, reason, token_count, messages_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (window_id, session_id, project_id, start_turn_no, end_turn_no, reason, token_count,
             json.dumps(messages, ensure_ascii=False)),
        )
        for member_no, trace_id in enumerate(dict.fromkeys(source_trace_ids), start=1):
            await conn.execute(
                "INSERT INTO trace_window_members (window_id, trace_id, member_no) VALUES (?, ?, ?)",
                (window_id, trace_id, member_no),
            )
        await conn.execute(
            "UPDATE session_trace_state SET last_captured_turn = ? WHERE session_id = ?",
            (end_turn_no, session_id),
        )
        await conn.commit()
        await conn.close()
        return {
            "window_id": window_id, "start_turn_no": start_turn_no, "end_turn_no": end_turn_no,
            "source_trace_ids": list(dict.fromkeys(source_trace_ids)), "messages": messages,
        }

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

    async def append_events(self, trace_id: str, events: list[dict]) -> list[str]:
        """Persist an ordered event batch in one SQLite transaction."""
        if not events:
            return []
        event_ids = [f"evt_{uuid.uuid4().hex}" for _ in events]
        created_at = _now()
        conn = await get_connection()
        await conn.executemany(
            """INSERT INTO trace_events
               (event_id, trace_id, parent_event_id, sequence_no, event_type, actor, payload_json, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    event_id, trace_id, event.get("parent_event_id"), event["sequence_no"],
                    event["event_type"], event["actor"],
                    json.dumps(event.get("payload") or {}, ensure_ascii=False, default=str),
                    event.get("duration_ms"), created_at,
                )
                for event_id, event in zip(event_ids, events)
            ],
        )
        await conn.commit()
        await conn.close()
        return event_ids

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

    async def get_trace_context(self, trace_id: str, before: int = 2, after: int = 2) -> dict | None:
        """Return one raw trace plus adjacent request traces from the same session."""
        trace = await self.get_trace(trace_id)
        if not trace:
            return None
        conn = await get_connection()
        turn_no = trace.get("turn_no")
        if turn_no is None:
            cursor = await conn.execute(
                "SELECT * FROM traces WHERE session_id = ? ORDER BY started_at DESC LIMIT ?",
                (trace["session_id"], max(1, before + after + 1)),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            await conn.close()
            result = {"trace": trace, "previous": [], "next": [], "related": rows}
            for item in rows:
                item["events"] = await self.list_events(item["trace_id"], limit=500)
            return result
        cursor = await conn.execute(
            """SELECT * FROM traces WHERE session_id = ? AND turn_no IS NOT NULL AND turn_no < ?
               ORDER BY turn_no DESC, started_at DESC LIMIT ?""",
            (trace["session_id"], turn_no, max(0, before) * 32),
        )
        previous_desc = []
        seen_turns = set()
        for row in await cursor.fetchall():
            item = dict(row)
            if item["turn_no"] in seen_turns:
                continue
            seen_turns.add(item["turn_no"])
            previous_desc.append(item)
            if len(previous_desc) >= max(0, before):
                break
        previous = list(reversed(previous_desc))
        cursor = await conn.execute(
            """SELECT * FROM traces WHERE session_id = ? AND turn_no IS NOT NULL AND turn_no > ?
               ORDER BY turn_no ASC, started_at ASC LIMIT ?""",
            (trace["session_id"], turn_no, max(0, after) * 32),
        )
        following = []
        seen_turns = set()
        for row in await cursor.fetchall():
            item = dict(row)
            if item["turn_no"] in seen_turns:
                continue
            seen_turns.add(item["turn_no"])
            following.append(item)
            if len(following) >= max(0, after):
                break

        # Context compression creates multiple Trace segments for one Session
        # turn. Include those directly linked same-turn segments as well.
        same_turn_previous = []
        linked_id = trace.get("previous_trace_id")
        for _ in range(32):
            if not linked_id:
                break
            cursor = await conn.execute("SELECT * FROM traces WHERE trace_id = ?", (linked_id,))
            linked_row = await cursor.fetchone()
            if linked_row is None:
                break
            linked = dict(linked_row)
            if linked.get("session_id") != trace["session_id"] or linked.get("turn_no") != turn_no:
                break
            same_turn_previous.append(linked)
            linked_id = linked.get("previous_trace_id")
        same_turn_previous.reverse()

        same_turn_following = []
        linked_id = trace.get("next_trace_id")
        for _ in range(32):
            if not linked_id:
                break
            cursor = await conn.execute("SELECT * FROM traces WHERE trace_id = ?", (linked_id,))
            linked_row = await cursor.fetchone()
            if linked_row is None:
                break
            linked = dict(linked_row)
            if linked.get("session_id") != trace["session_id"] or linked.get("turn_no") != turn_no:
                break
            same_turn_following.append(linked)
            linked_id = linked.get("next_trace_id")

        previous_ids = {item["trace_id"] for item in previous}
        previous.extend(item for item in same_turn_previous if item["trace_id"] not in previous_ids)
        following_ids = {item["trace_id"] for item in following}
        following = [
            *[item for item in same_turn_following if item["trace_id"] not in following_ids],
            *following,
        ]
        await conn.close()
        result = {"trace": trace, "previous": previous, "next": following}
        for item in [trace, *previous, *following]:
            item["events"] = await self.list_events(item["trace_id"], limit=500)
        return result

    async def get_trace_window(self, window_id: str) -> dict | None:
        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM trace_analysis_windows WHERE window_id = ?", (window_id,))
        row = await cursor.fetchone()
        if row is None:
            await conn.close()
            return None
        result = dict(row)
        result["messages"] = json.loads(result.pop("messages_json") or "[]")
        result["normalized_trajectory"] = json.loads(
            result.pop("normalized_trajectory_json", "[]") or "[]"
        )
        result["trajectory_provenance"] = json.loads(
            result.pop("trajectory_provenance_json", "[]") or "[]"
        )
        result["prepared_payload"] = json.loads(
            result.pop("prepared_payload_json", "{}") or "{}"
        )
        cursor = await conn.execute(
            "SELECT trace_id FROM trace_window_members WHERE window_id = ? ORDER BY member_no ASC", (window_id,)
        )
        result["source_trace_ids"] = [str(member[0]) for member in await cursor.fetchall()]
        await conn.close()
        return result

    async def save_trace_window_trajectory(self, window_id: str, payload: dict,
                                           trajectory_trace_id: str = "") -> None:
        """Persist the validated Memory/Summary first-pass trajectory for reuse."""
        conn = await get_connection()
        await conn.execute(
            """UPDATE trace_analysis_windows
               SET normalized_trajectory_json = ?, trajectory_provenance_json = ?,
                   trajectory_trace_id = ?, normalized_at = ?
               WHERE window_id = ?""",
            (
                json.dumps(payload.get("trajectory", []), ensure_ascii=False),
                json.dumps(payload.get("provenance", []), ensure_ascii=False),
                trajectory_trace_id,
                _now(),
                window_id,
            ),
        )
        await conn.execute(
            "UPDATE trace_analysis_windows SET status = 'normalized', last_error = '' WHERE window_id = ?",
            (window_id,),
        )
        await conn.commit()
        await conn.close()

    async def save_trace_window_prepared(self, window_id: str, payload: dict) -> None:
        """Persist the final validated write plan before any file-backed side effect."""
        conn = await get_connection()
        await conn.execute(
            """UPDATE trace_analysis_windows
               SET status = 'prepared', prepared_payload_json = ?, prepared_at = ?, last_error = ''
               WHERE window_id = ?""",
            (json.dumps(payload, ensure_ascii=False), _now(), window_id),
        )
        await conn.commit()
        await conn.close()

    async def record_trace_window_failure(self, window_id: str, error: str, max_retries: int = 3) -> dict:
        """Record a bounded retry using SQLite time so restart recovery is deterministic."""
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT retry_count FROM trace_analysis_windows WHERE window_id = ?", (window_id,)
        )
        row = await cursor.fetchone()
        retry_count = int(row[0]) + 1 if row else 1
        delay_seconds = min(300, 5 * (2 ** max(0, retry_count - 1)))
        status = "failed" if retry_count >= max_retries else "retry_wait"
        await conn.execute(
            """UPDATE trace_analysis_windows
               SET status = ?, retry_count = ?, last_error = ?,
                   next_retry_at = datetime('now', ?), finished_at = ?
               WHERE window_id = ?""",
            (
                status, retry_count, str(error)[:8000], f"+{delay_seconds} seconds",
                _now() if status == "failed" else None, window_id,
            ),
        )
        await conn.commit()
        await conn.close()
        return {"status": status, "retry_count": retry_count, "delay_seconds": delay_seconds}

    async def list_recoverable_trace_windows(self, max_retries: int = 3,
                                             include_waiting: bool = False) -> list[dict]:
        """Return durable work that was never completed, including interrupted running stages."""
        conn = await get_connection()
        waiting_clause = "" if include_waiting else "AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))"
        cursor = await conn.execute(
            f"""SELECT window_id, project_id, status,
                       CAST(MAX(0, (julianday(next_retry_at) - julianday('now')) * 86400) AS INTEGER) AS delay_seconds
                FROM trace_analysis_windows
                WHERE status IN ('pending', 'running', 'normalized', 'prepared', 'retry_wait')
                  AND retry_count < ? {waiting_clause}
                ORDER BY project_id, start_turn_no, created_at""",
            (max_retries,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows

    async def get_latest_trace_window_summary(self, session_id: str) -> dict | None:
        """Return the newest completed per-window summary for one Session."""
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT * FROM trace_analysis_windows
               WHERE session_id = ? AND status = 'complete' AND summary != ''
               ORDER BY COALESCE(summary_end_turn_no, end_turn_no) DESC, finished_at DESC LIMIT 1""",
            (session_id,),
        )
        row = await cursor.fetchone()
        await conn.close()
        return dict(row) if row else None

    async def list_trace_window_summaries_after(self, session_id: str, turn_no: int) -> list[dict]:
        """Return every completed per-window Memory summary after a compression boundary."""
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT window_id, start_turn_no, end_turn_no, summary,
                      summary_trace_id, summary_start_turn_no, summary_end_turn_no
               FROM trace_analysis_windows
               WHERE session_id = ? AND status = 'complete' AND summary != ''
                 AND COALESCE(summary_start_turn_no, start_turn_no) > ?
               ORDER BY COALESCE(summary_start_turn_no, start_turn_no), created_at""",
            (session_id, max(0, turn_no)),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows

    async def get_session_compression_state(self, session_id: str) -> dict:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM session_compression_state WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        await conn.close()
        if row is None:
            return {"session_id": session_id, "last_compressed_turn_no": 0, "messages": []}
        result = dict(row)
        result["messages"] = json.loads(result.pop("messages_json") or "[]")
        return result

    async def save_session_compression_state(self, session_id: str, last_turn_no: int,
                                             messages: list[dict]) -> None:
        conn = await get_connection()
        await conn.execute(
            """INSERT INTO session_compression_state
               (session_id, last_compressed_turn_no, messages_json, compressed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   last_compressed_turn_no = excluded.last_compressed_turn_no,
                   messages_json = excluded.messages_json,
                   compressed_at = excluded.compressed_at""",
            (session_id, max(0, last_turn_no), json.dumps(messages, ensure_ascii=False), _now()),
        )
        await conn.commit()
        await conn.close()

    async def list_session_turns(self, session_id: str, after_turn: int = 0) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT turn_no, user_content, assistant_content, source_trace_id, created_at
               FROM session_trace_turns WHERE session_id = ? AND turn_no > ? ORDER BY turn_no""",
            (session_id, max(0, after_turn)),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        return rows

    async def list_trace_window_events(self, window_id: str, limit: int = 2_000) -> list[dict]:
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT e.*, m.member_no FROM trace_window_members m
               JOIN trace_events e ON e.trace_id = m.trace_id
               WHERE m.window_id = ?
               ORDER BY m.member_no ASC, e.sequence_no ASC LIMIT ?""",
            (window_id, limit),
        )
        rows = await cursor.fetchall()
        await conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    async def set_trace_window_status(self, window_id: str, status: str) -> None:
        conn = await get_connection()
        finished_at = _now() if status in {"complete", "failed"} else None
        await conn.execute(
            "UPDATE trace_analysis_windows SET status = ?, finished_at = ? WHERE window_id = ?",
            (status, finished_at, window_id),
        )
        await conn.commit()
        await conn.close()

    async def complete_trace_window_summary(self, window_id: str, summary: str,
                                            summary_trace_id: str = "") -> None:
        """Finish a memory-analysis window with its own per-window summary."""
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT start_turn_no, end_turn_no FROM trace_analysis_windows WHERE window_id = ?", (window_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.close()
            return
        start_turn_no, end_turn_no = int(row[0]), int(row[1])
        await conn.execute(
            """UPDATE trace_analysis_windows
               SET status = 'complete', summary = ?, summary_trace_id = ?,
                   summary_start_turn_no = ?, summary_end_turn_no = ?, finished_at = ?,
                   last_error = '', next_retry_at = NULL
               WHERE window_id = ?""",
            (summary, summary_trace_id, start_turn_no, end_turn_no, _now(), window_id),
        )
        await conn.commit()
        await conn.close()

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

    async def list_project_traces_page(
        self, project_id: str, *, search: str = "", session_id: str = "",
        status: str = "", operation_kind: str = "", analysis_status: str = "", source_agent: str = "",
        limit: int = 25, offset: int = 0,
    ) -> dict:
        """Return a filtered Trace page for the read-only management UI."""
        clauses = ["traces.project_id = ?"]
        params: list[object] = [project_id]
        if search:
            clauses.append(
                "(traces.trace_id LIKE ? OR traces.session_id LIKE ? OR traces.user_message LIKE ?)"
            )
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        if session_id:
            clauses.append("traces.session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("traces.status = ?")
            params.append(status)
        if operation_kind:
            clauses.append("traces.operation_kind = ?")
            params.append(operation_kind)
        if analysis_status:
            clauses.append("traces.analysis_status = ?")
            params.append(analysis_status)
        if source_agent:
            clauses.append("traces.source_agent = ?")
            params.append(source_agent)
        where = " AND ".join(clauses)
        conn = await get_connection()
        cursor = await conn.execute(f"SELECT COUNT(*) FROM traces WHERE {where}", params)
        total_row = await cursor.fetchone()
        cursor = await conn.execute(
            f"""SELECT traces.*, trace_classifications.trace_id AS classified_trace_id,
                       trace_classifications.has_error, trace_classifications.has_correction,
                       trace_classifications.has_confirmation, trace_classifications.has_feedback,
                       COUNT(trace_events.event_id) AS event_count
                FROM traces
                LEFT JOIN trace_classifications ON trace_classifications.trace_id = traces.trace_id
                LEFT JOIN trace_events ON trace_events.trace_id = traces.trace_id
                WHERE {where}
                GROUP BY traces.trace_id
                ORDER BY traces.started_at DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await conn.close()
        for row in rows:
            row["is_classified"] = bool(row.pop("classified_trace_id", None))
            for key in ("has_error", "has_correction", "has_confirmation", "has_feedback"):
                row[key] = bool(row.get(key))
        return {"items": rows, "total": int(total_row[0]) if total_row else 0,
                "limit": limit, "offset": offset}

    async def get_project_trace_agent_stats(self, project_id: str) -> dict:
        """Count primary source Agents and all Agent participants in project Traces."""
        conn = await get_connection()
        cursor = await conn.execute(
            """SELECT COALESCE(NULLIF(source_agent, ''), 'unknown') AS agent, COUNT(*) AS count
               FROM traces WHERE project_id = ? GROUP BY agent ORDER BY count DESC, agent""",
            (project_id,),
        )
        sources = [{"agent": str(row[0]), "count": int(row[1])} for row in await cursor.fetchall()]
        cursor = await conn.execute(
            """SELECT trace_events.actor, COUNT(DISTINCT trace_events.trace_id) AS trace_count
               FROM trace_events JOIN traces ON traces.trace_id = trace_events.trace_id
               WHERE traces.project_id = ?
                 AND trace_events.actor NOT IN ('system', 'user', 'tool', '')
               GROUP BY trace_events.actor ORDER BY trace_count DESC, trace_events.actor""",
            (project_id,),
        )
        participants = [{"agent": str(row[0]), "count": int(row[1])} for row in await cursor.fetchall()]
        await conn.close()
        return {"sources": sources, "participants": participants,
                "total": sum(item["count"] for item in sources)}

    async def validate_trace_chain(self, trace_id: str) -> dict | None:
        """Check one Trace's lineage, event sequence and Session-turn anchor."""
        conn = await get_connection()

        async def load(item_id: str) -> dict | None:
            cursor = await conn.execute("SELECT * FROM traces WHERE trace_id = ?", (item_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

        trace = await load(trace_id)
        if trace is None:
            await conn.close()
            return None
        checks: list[dict] = []
        chain_before: list[dict] = []
        chain_after: list[dict] = []
        visited = {trace_id}
        current = trace
        valid = True

        for direction, target in (("previous", chain_before), ("next", chain_after)):
            current = trace
            local: list[dict] = []
            for _ in range(64):
                linked_id = current.get(f"{direction}_trace_id")
                if not linked_id:
                    break
                if linked_id in visited:
                    checks.append({"key": f"{direction}_cycle", "status": "fail", "message": "Trace 链存在循环引用"})
                    valid = False
                    break
                linked = await load(str(linked_id))
                if linked is None:
                    checks.append({"key": f"{direction}_missing", "status": "fail", "message": f"关联 Trace 不存在：{linked_id}"})
                    valid = False
                    break
                reciprocal = "next_trace_id" if direction == "previous" else "previous_trace_id"
                if linked.get(reciprocal) != current["trace_id"]:
                    checks.append({"key": f"{direction}_reciprocal", "status": "fail", "message": f"{linked_id} 的反向引用不一致"})
                    valid = False
                if any(linked.get(key) != trace.get(key) for key in ("session_id", "project_id")):
                    checks.append({"key": f"{direction}_scope", "status": "fail", "message": f"{linked_id} 的 Session 或项目与当前链不一致"})
                    valid = False
                visited.add(str(linked_id))
                local.append(linked)
                current = linked
            if direction == "previous":
                chain_before.extend(reversed(local))
            else:
                chain_after.extend(local)

        if valid:
            checks.append({"key": "lineage", "status": "pass", "message": "前后 Trace 引用一致"})

        cursor = await conn.execute(
            "SELECT COUNT(*), MIN(sequence_no), MAX(sequence_no) FROM trace_events WHERE trace_id = ?",
            (trace_id,),
        )
        event_row = await cursor.fetchone()
        event_count = int(event_row[0]) if event_row else 0
        sequence_valid = bool(event_count and int(event_row[1]) == 1 and int(event_row[2]) == event_count)
        checks.append({
            "key": "event_sequence", "status": "pass" if sequence_valid else "fail",
            "message": f"事件序号连续（共 {event_count} 条）" if sequence_valid else f"事件序号不连续或为空（共 {event_count} 条）",
        })
        valid = valid and sequence_valid

        # previous/next also links ordinary consecutive Session turns.  The
        # Session-turn anchor must point to the final segment of this turn,
        # rather than the final Trace of the whole Session.
        terminal = trace
        for item in chain_after:
            if item.get("turn_no") != trace.get("turn_no"):
                break
            terminal = item
        turn_no = trace.get("turn_no")
        if turn_no is not None:
            cursor = await conn.execute(
                "SELECT source_trace_id FROM session_trace_turns WHERE session_id = ? AND turn_no = ?",
                (trace["session_id"], turn_no),
            )
            source_row = await cursor.fetchone()
            source_id = str(source_row[0]) if source_row and source_row[0] else ""
            anchor_valid = source_id == terminal["trace_id"]
            checks.append({
                "key": "turn_anchor", "status": "pass" if anchor_valid else "fail",
                "message": "Session Turn 指向链尾 Trace" if anchor_valid else f"Session Turn 指向 {source_id or '空值'}，链尾为 {terminal['trace_id']}",
            })
            valid = valid and anchor_valid
        else:
            checks.append({"key": "turn_anchor", "status": "warning", "message": "该 Trace 没有 Turn 编号，未校验 Session Turn 锚点"})

        await conn.close()
        return {
            "trace_id": trace_id, "valid": valid,
            "chain_trace_ids": [item["trace_id"] for item in [*chain_before, trace, *chain_after]],
            "terminal_trace_id": terminal["trace_id"], "checks": checks,
        }

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

    async def count_events(self, trace_id: str) -> int:
        conn = await get_connection()
        cursor = await conn.execute("SELECT COUNT(*) FROM trace_events WHERE trace_id = ?", (trace_id,))
        row = await cursor.fetchone()
        await conn.close()
        return int(row[0]) if row else 0

    @staticmethod
    def annotate_event_turns(events: list[dict]) -> list[dict]:
        """Attach the nearest ReAct turn to every event without changing its payload.

        Tool results and user answers do not currently carry ``payload.turn``.
        They inherit the nearest explicit turn in the same Trace; a following
        turn wins a tie because an answer between two LLM calls belongs to the
        call that consumes it.  Historical imports use ``turn_no`` instead.
        """
        grouped: dict[str, list[dict]] = {}
        for event in events:
            grouped.setdefault(str(event.get("trace_id") or ""), []).append(event)

        annotated: list[dict] = []
        for trace_events in grouped.values():
            anchors: list[tuple[int, int]] = []
            for event in trace_events:
                payload = event.get("payload") or {}
                raw_turn = payload.get("turn")
                if raw_turn is None:
                    raw_turn = payload.get("turn_no")
                try:
                    anchors.append((int(event.get("sequence_no") or 0), int(raw_turn)))
                except (TypeError, ValueError):
                    continue

            for event in trace_events:
                item = dict(event)
                sequence = int(event.get("sequence_no") or 0)
                payload = event.get("payload") or {}
                raw_turn = payload.get("turn")
                if raw_turn is None:
                    raw_turn = payload.get("turn_no")
                try:
                    item["trace_turn"] = int(raw_turn)
                except (TypeError, ValueError):
                    if anchors:
                        item["trace_turn"] = min(
                            anchors,
                            key=lambda anchor: (abs(anchor[0] - sequence), 0 if anchor[0] >= sequence else 1),
                        )[1]
                    else:
                        item["trace_turn"] = 1
                annotated.append(item)
        return sorted(annotated, key=lambda event: (
            int(event.get("member_no") or 0), int(event.get("sequence_no") or 0),
        ))

    async def get_trace_turn_range(self, trace_id: str, start_turn: int, end_turn: int,
                                   limit: int = 300) -> list[dict]:
        """Return one inclusive, bounded ReAct-turn range from a Trace."""
        events = self.annotate_event_turns(await self.list_events(trace_id, limit=5_000))
        selected = [
            event for event in events
            if start_turn <= int(event.get("trace_turn") or 0) <= end_turn
        ]
        return selected[:max(1, limit)]

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
