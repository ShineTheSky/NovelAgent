"""Safe, evidence-based migration for legacy Agent Bad Case Trace links."""

import json
from datetime import datetime, timezone

from novelagent.trace.agent_bad_cases import GLOBAL_PROJECT_ID
from novelagent.storage.database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_values(payload: dict) -> set[str]:
    """Extract the error-bearing fields used by the older recorder."""
    return {
        str(payload.get(key) or "").strip()
        for key in ("message", "error", "data", "content")
        if str(payload.get(key) or "").strip()
    }


async def migrate_bad_case_trace_links() -> dict:
    """Link only deterministically provable legacy Bad Cases.

    Source Markdown and old Trace event payloads are intentionally untouched.
    A link is accepted only when the old source id directly resolves, or when
    exactly one Trace in the recorded session/project contains the same error
    text.  Time proximity is deliberately not used as proof.
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT t.trace_id, e.payload_json
               FROM traces t JOIN trace_events e ON e.trace_id = t.trace_id
               WHERE t.project_id = ? AND e.event_type = 'bad_case_context'
               ORDER BY t.started_at ASC""",
            (GLOBAL_PROJECT_ID,),
        )
        cases = await cursor.fetchall()
        result = {"resolved_direct": 0, "resolved_event_match": 0, "unmapped_legacy": 0, "skipped": 0}
        for bad_case_trace_id, raw_metadata in cases:
            try:
                metadata = json.loads(raw_metadata or "{}")
            except json.JSONDecodeError:
                result["skipped"] += 1
                continue
            legacy_source_id = str(metadata.get("source_trace_id") or "")
            session_id = str(metadata.get("source_session_id") or "")
            project_id = str(metadata.get("source_project_id") or "")
            error_text = str(metadata.get("error") or "").strip()
            resolved_trace_id = None
            status = "unmapped_legacy"
            match_method = ""
            candidates: list[str] = []

            if legacy_source_id:
                cursor = await conn.execute(
                    """SELECT trace_id FROM traces
                       WHERE trace_id = ? AND session_id = ? AND project_id = ?""",
                    (legacy_source_id, session_id, project_id),
                )
                direct = await cursor.fetchone()
                if direct:
                    resolved_trace_id = str(direct[0])
                    status, match_method = "resolved", "source_trace_id"
                    result["resolved_direct"] += 1

            if not resolved_trace_id and error_text and session_id and project_id:
                cursor = await conn.execute(
                    """SELECT t.trace_id, e.payload_json
                       FROM traces t JOIN trace_events e ON e.trace_id = t.trace_id
                       WHERE t.session_id = ? AND t.project_id = ? AND t.project_id != ?""",
                    (session_id, project_id, GLOBAL_PROJECT_ID),
                )
                matches: set[str] = set()
                for candidate_trace_id, raw_event in await cursor.fetchall():
                    try:
                        payload = json.loads(raw_event or "{}")
                    except json.JSONDecodeError:
                        continue
                    if error_text in _text_values(payload):
                        matches.add(str(candidate_trace_id))
                candidates = sorted(matches)
                if len(candidates) == 1:
                    resolved_trace_id = candidates[0]
                    status, match_method = "resolved", "exact_error_event"
                    result["resolved_event_match"] += 1

            if not resolved_trace_id:
                result["unmapped_legacy"] += 1
            await conn.execute(
                """INSERT INTO bad_case_trace_links
                   (bad_case_trace_id, legacy_source_trace_id, resolved_trace_id, status, match_method,
                    candidate_trace_ids_json, linked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bad_case_trace_id) DO UPDATE SET
                       legacy_source_trace_id = excluded.legacy_source_trace_id,
                       resolved_trace_id = excluded.resolved_trace_id,
                       status = excluded.status,
                       match_method = excluded.match_method,
                       candidate_trace_ids_json = excluded.candidate_trace_ids_json,
                       linked_at = excluded.linked_at""",
                (bad_case_trace_id, legacy_source_id, resolved_trace_id, status, match_method,
                 json.dumps(candidates, ensure_ascii=False), _now()),
            )
        await conn.commit()
        return result
    finally:
        await conn.close()


async def list_bad_case_trace_links() -> list[dict]:
    """Read migration outcomes without inspecting or modifying Bad Case payloads."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT bad_case_trace_id, legacy_source_trace_id, resolved_trace_id, status,
                      match_method, candidate_trace_ids_json
               FROM bad_case_trace_links ORDER BY linked_at ASC"""
        )
        rows = []
        for row in await cursor.fetchall():
            rows.append({
                "bad_case_trace_id": row[0], "legacy_source_trace_id": row[1],
                "resolved_trace_id": row[2], "status": row[3], "match_method": row[4],
                "candidate_trace_ids": json.loads(row[5] or "[]"),
            })
        return rows
    finally:
        await conn.close()
