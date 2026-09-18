"""Compact legacy streamed Trace chunks in SQLite.

Dry-run by default. Use --apply to create a timestamped backup and rewrite the
three persisted event stores: trace_events, session_trace_turns and
session_display_turns.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from novelagent.trace.stream_compaction import (
    compact_display_events,
    compact_trace_snapshot_events,
    merge_stream_payload,
    stream_signature,
)


DEFAULT_DB = ROOT / "data" / "novelagent.db"


def compact_trace_rows(rows: list[sqlite3.Row]) -> tuple[list[dict], dict[str, str]]:
    compacted: list[dict] = []
    id_map: dict[str, str] = {}
    previous_signature = None
    previous_trace_id = ""
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        signature = stream_signature(
            item["event_type"], item["actor"], item.get("parent_event_id"), item["payload"],
        )
        if (
            compacted and item["trace_id"] == previous_trace_id
            and signature is not None and signature == previous_signature
        ):
            target = compacted[-1]
            merge_stream_payload(target["payload"], item["payload"], item["event_type"])
            if item.get("duration_ms") is not None:
                target["duration_ms"] = (target.get("duration_ms") or 0) + item["duration_ms"]
            id_map[item["event_id"]] = target["event_id"]
            continue
        compacted.append(item)
        previous_trace_id = item["trace_id"]
        previous_signature = signature

    sequence_by_trace: dict[str, int] = {}
    for item in compacted:
        trace_id = item["trace_id"]
        sequence_by_trace[trace_id] = sequence_by_trace.get(trace_id, 0) + 1
        item["sequence_no"] = sequence_by_trace[trace_id]
    for item in compacted:
        parent = item.get("parent_event_id")
        if parent in id_map:
            item["parent_event_id"] = id_map[parent]
    return compacted, id_map


def remap_json(value, id_map: dict[str, str]):
    if isinstance(value, str):
        return id_map.get(value, value)
    if isinstance(value, list):
        return [remap_json(item, id_map) for item in value]
    if isinstance(value, dict):
        return {key: remap_json(item, id_map) for key, item in value.items()}
    return value


def compact_json_column(conn: sqlite3.Connection, table: str, key_columns: list[str],
                        column: str, transform) -> tuple[int, int]:
    selected = ", ".join([*key_columns, column])
    rows = conn.execute(f"SELECT {selected} FROM {table}").fetchall()
    before = after = 0
    for row in rows:
        raw = row[column] or "[]"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            before += len(value)
        updated = transform(value)
        if isinstance(updated, list):
            after += len(updated)
        where = " AND ".join(f"{key} = ?" for key in key_columns)
        params = [json.dumps(updated, ensure_ascii=False), *[row[key] for key in key_columns]]
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE {where}", params)
    return before, after


def rewrite_trace_events(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.execute("DROP TABLE IF EXISTS trace_events_compacted")
    conn.execute("""CREATE TABLE trace_events_compacted (
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
    )""")
    conn.executemany(
        """INSERT INTO trace_events_compacted
           (event_id, trace_id, parent_event_id, sequence_no, event_type, actor, payload_json, duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(
            item["event_id"], item["trace_id"], item.get("parent_event_id"), item["sequence_no"],
            item["event_type"], item["actor"], json.dumps(item["payload"], ensure_ascii=False),
            item.get("duration_ms"), item["created_at"],
        ) for item in rows],
    )
    conn.execute("DROP TABLE trace_events")
    conn.execute("ALTER TABLE trace_events_compacted RENAME TO trace_events")
    conn.execute("CREATE INDEX idx_trace_events_trace ON trace_events(trace_id, sequence_no)")


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.stem}.before-stream-compaction-{stamp}{db_path.suffix}")
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
    return backup_path


def run(db_path: Path, apply: bool) -> dict:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY trace_id, sequence_no").fetchall()
    compacted, id_map = compact_trace_rows(rows)
    report = {
        "database": str(db_path),
        "trace_events_before": len(rows),
        "trace_events_after": len(compacted),
        "trace_events_removed": len(rows) - len(compacted),
        "remapped_event_ids": len(id_map),
        "apply": apply,
    }
    if not apply:
        conn.close()
        return report

    conn.close()
    backup_path = backup_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock so an event appended between the dry
        # calculation and migration cannot be dropped.
        rows = conn.execute("SELECT * FROM trace_events ORDER BY trace_id, sequence_no").fetchall()
        compacted, id_map = compact_trace_rows(rows)
        report.update({
            "trace_events_before": len(rows),
            "trace_events_after": len(compacted),
            "trace_events_removed": len(rows) - len(compacted),
            "remapped_event_ids": len(id_map),
        })
        rewrite_trace_events(conn, compacted)
        trace_before, trace_after = compact_json_column(
            conn, "session_trace_turns", ["session_id", "turn_no"], "events_json",
            lambda value: compact_trace_snapshot_events(remap_json(value, id_map)) if isinstance(value, list) else value,
        )
        display_before, display_after = compact_json_column(
            conn, "session_display_turns", ["session_id", "turn_no"], "events_json",
            lambda value: compact_display_events(value) if isinstance(value, list) else value,
        )
        for table, key, column in (
            ("trace_classifications", "trace_id", "items_json"),
            ("trace_memories", "memory_id", "source_event_ids_json"),
            ("trace_memory_evidence", "evidence_id", "source_event_ids_json"),
        ):
            compact_json_column(
                conn, table, [key], column,
                lambda value, mapping=id_map: remap_json(value, mapping),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.execute("VACUUM")
    conn.close()
    report.update({
        "backup": str(backup_path),
        "session_trace_events_before": trace_before,
        "session_trace_events_after": trace_after,
        "display_events_before": display_before,
        "display_events_after": display_after,
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.db.resolve(), args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
