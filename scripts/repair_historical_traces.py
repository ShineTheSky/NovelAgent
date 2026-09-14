"""Repair corrupted historical Trace text from the intact session transcript."""

import argparse
import json
import sqlite3

from novelagent.trace.store import _extract_historical_turns


def expected_events(trace_id: str, batch: list[dict]) -> list[tuple[str, str, str]]:
    events = []
    for turn in batch:
        events.append(("user_message", "user", json.dumps({
            "source": "historical", "turn_no": turn["turn_no"],
            "message_start": turn["message_start"], "message_end": turn["message_end"],
            "content": turn["user_content"],
        }, ensure_ascii=False)))
        if turn["tool_activity"]:
            events.append(("historical_tool_activity", "system", json.dumps({
                "source": "historical", "turn_no": turn["turn_no"], "tools": turn["tool_activity"],
            }, ensure_ascii=False)))
        events.append(("assistant_turn", "main_agent", json.dumps({
            "source": "historical", "turn_no": turn["turn_no"], "content": turn["assistant_content"],
        }, ensure_ascii=False)))
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect("data/novelagent.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT t.trace_id, t.session_id, s.messages_json, ts.start_turn_no, ts.end_turn_no
           FROM traces t JOIN sessions s ON s.session_id = t.session_id
           JOIN trace_snapshots ts ON ts.trace_id = t.trace_id
           WHERE t.source = 'historical' ORDER BY t.session_id, ts.start_turn_no"""
    ).fetchall()
    repaired = mismatched = 0
    for row in rows:
        messages = json.loads(row["messages_json"] or "[]")
        turns, _ = _extract_historical_turns(messages)
        batch = [turn for turn in turns if row["start_turn_no"] <= turn["turn_no"] <= row["end_turn_no"]]
        events = expected_events(row["trace_id"], batch)
        existing = conn.execute(
            "SELECT event_id FROM trace_events WHERE trace_id = ? ORDER BY sequence_no", (row["trace_id"],)
        ).fetchall()
        if not batch or len(events) != len(existing):
            mismatched += 1
            print({"trace_id": row["trace_id"], "status": "skipped", "expected": len(events), "actual": len(existing)})
            continue
        repaired += 1
        if not args.apply:
            continue
        for sequence, (event, values) in enumerate(zip(existing, events), start=1):
            event_type, actor, payload = values
            conn.execute(
                "UPDATE trace_events SET sequence_no=?, event_type=?, actor=?, payload_json=? WHERE event_id=?",
                (sequence, event_type, actor, payload, event["event_id"]),
            )
        snapshot = [message for turn in batch for message in turn["raw_messages"]]
        conn.execute(
            "UPDATE traces SET user_message=?, final_answer=? WHERE trace_id=?",
            (batch[-1]["user_content"], batch[-1]["assistant_content"], row["trace_id"]),
        )
        conn.execute(
            "UPDATE trace_snapshots SET messages_json=? WHERE trace_id=?",
            (json.dumps(snapshot, ensure_ascii=False), row["trace_id"]),
        )
    if args.apply:
        conn.commit()
    conn.close()
    print({"mode": "apply" if args.apply else "dry_run", "repaired": repaired, "mismatched": mismatched})


if __name__ == "__main__":
    main()
