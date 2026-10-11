"""Archive and remove billing Bad Cases; preview by default, no model calls.

Original task traces are preserved. Only failed billing-only bad_case_analyzer
runs may be removed with their cases, provided no surviving evidence refers to them.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from novelagent.llm.failures import is_balance_error

CHILDREN = {
    "trace_events": "trace_id IN (SELECT id FROM purge_ids)",
    "trace_classifications": "trace_id IN (SELECT id FROM purge_ids)",
    "trace_snapshots": "trace_id IN (SELECT id FROM purge_ids)",
    "bad_case_trace_links": "bad_case_trace_id IN (SELECT id FROM purge_ids) OR resolved_trace_id IN (SELECT id FROM purge_ids)",
}
PROTECTED = {
    "trace_memories": ("trace_id",), "trace_memory_evidence": ("trace_id",),
    "trace_window_members": ("trace_id",), "session_trace_turns": ("source_trace_id",),
    "trace_analysis_windows": ("summary_trace_id", "trajectory_trace_id"),
}


def plan(conn, case_dir):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cases, sources, survivors = set(), set(), set()
    for trace_id, in conn.execute("SELECT trace_id FROM traces WHERE operation_kind='agent_bad_case'").fetchall():
        row = conn.execute("SELECT payload_json FROM trace_events WHERE trace_id=? AND event_type='bad_case_context'", (trace_id,)).fetchone()
        if not row:
            continue
        metadata = json.loads(row[0])
        source = metadata.get("source_trace_id", "")
        if is_balance_error(metadata.get("error", "")):
            cases.add(trace_id)
            sources.add(source)
        else:
            survivors.add(source)
    runs = set()
    for source in sources - survivors - {""}:
        row = conn.execute("SELECT operation_kind,source_agent,status FROM traces WHERE trace_id=?", (source,)).fetchone()
        if not row or tuple(row) != ("agent_run", "bad_case_analyzer", "failed"):
            continue
        errors = [json.loads(r[0]).get("error", "") for r in conn.execute(
            "SELECT payload_json FROM trace_events WHERE trace_id=? AND event_type='agent_finished'", (source,))]
        if errors and all(is_balance_error(error) for error in errors):
            runs.add(source)
    # Do not remove evidence still used by a conversation, History analysis, or knowledge.
    protected = set()
    for trace_id in cases | runs:
        for table, columns in PROTECTED.items():
            if table in tables and any(conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (trace_id,)).fetchone() for column in columns):
                protected.add(trace_id)
        if conn.execute("SELECT 1 FROM traces WHERE previous_trace_id=? OR next_trace_id=? LIMIT 1", (trace_id, trace_id)).fetchone():
            protected.add(trace_id)
    cases -= protected
    # A retained case still requires its source run.
    for trace_id in protected:
        row = conn.execute("SELECT payload_json FROM trace_events WHERE trace_id=? AND event_type='bad_case_context'", (trace_id,)).fetchone()
        if row:
            runs.discard(json.loads(row[0]).get("source_trace_id", ""))
    if "bad_case_trace_links" in tables:
        for case_id, source in conn.execute("SELECT bad_case_trace_id,resolved_trace_id FROM bad_case_trace_links"):
            if case_id not in cases:
                runs.discard(source)
    runs -= protected
    conn.execute("CREATE TEMP TABLE purge_ids(id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO purge_ids VALUES (?)", [(t,) for t in sorted(cases | runs)])
    files = []
    for path in sorted(case_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        try:
            _, header, body = text.split("---", 2)
            metadata = yaml.safe_load(header) or {}
        except (ValueError, yaml.YAMLError):
            continue
        error = re.search(r"## 错误\s*(.*?)(?=\n## |\Z)", body, re.S)
        if metadata.get("id") in protected:
            continue
        if metadata.get("id") in cases or (error and is_balance_error(error[1])):
            files.append(path)
    for path in sorted((case_dir / "analysis").glob("*.md")):
        try:
            metadata = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1]) or {}
        except (ValueError, IndexError, yaml.YAMLError):
            continue
        if set(metadata.get("source_case_ids", [])) & cases:
            files.append(path)  # Remove stale summaries of cases being deleted.
    where = {**{t: w for t, w in CHILDREN.items() if t in tables}, "traces": "trace_id IN (SELECT id FROM purge_ids)"}
    counts = {t: conn.execute(f"SELECT count(*) FROM {t} WHERE {w}").fetchone()[0] for t, w in where.items()}
    return {"balance_cases": len(cases), "analysis_runs": len(runs), "protected_ids": sorted(protected),
            "files": files, "ids": sorted(cases | runs), "rows": counts, "where": where}


def cleanup(db_path, *, apply=False, vacuum=False):
    db_path = Path(db_path).resolve(strict=True)
    case_dir = db_path.parent / "agent_bad_cases"
    conn = sqlite3.connect(db_path if apply else db_path.as_uri() + "?mode=ro", uri=not apply, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    archive = None
    try:
        if apply:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
        selected = plan(conn, case_dir)
        report = {k: selected[k] for k in ("balance_cases", "analysis_runs", "protected_ids", "rows")}
        report.update(mode="apply" if apply else "preview", files=len(selected["files"]), before_bytes=db_path.stat().st_size)
        if not apply or not (selected["ids"] or selected["files"]):
            return report
        archive_dir = db_path.parent / "cleanup_archives"
        archive_dir.mkdir(exist_ok=True)
        archive = archive_dir / ("balance-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + ".zip")
        file_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in selected["files"]}
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as backup:
            manifest = {**report, "database": str(db_path), "trace_ids": selected["ids"],
                        "file_sha256": {p.relative_to(case_dir).as_posix(): h for p, h in file_hashes.items()},
                        "schemas": {t: conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0] for t in selected["where"]}}
            backup.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            with backup.open("database.jsonl", "w", force_zip64=True) as output:
                for table, where in selected["where"].items():
                    print(json.dumps({"archiving_table": table, "rows": selected["rows"][table]}), flush=True)
                    for index, row in enumerate(conn.execute(f"SELECT * FROM {table} WHERE {where}"), 1):
                        output.write((json.dumps({"table": table, "row": dict(row)}, ensure_ascii=False) + "\n").encode())
                        if index % 10_000 == 0:
                            print(json.dumps({"archiving_table": table, "archived_rows": index}), flush=True)
            for path, digest in file_hashes.items():
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(case_dir.resolve()) or resolved != path:
                    raise ValueError("Bad Case file escapes the exact cleanup directory")
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("Bad Case file changed during archival")
                backup.write(path, "files/" + path.relative_to(case_dir).as_posix())
        # The archive must be complete and closed before any production deletion.
        with zipfile.ZipFile(archive) as verified:
            if verified.testzip() is not None:
                raise ValueError("Cleanup archive checksum failed; nothing deleted")
        for table, where in selected["where"].items():
            conn.execute(f"DELETE FROM {table} WHERE {where}")
        conn.commit()
        for path, digest in file_hashes.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Bad Case changed after commit; left on disk, archive retained")
            path.unlink()
        if vacuum:
            print("Compacting database after committed cleanup.", flush=True)
            conn.execute("VACUUM")
        report.update(archive=str(archive), archive_bytes=archive.stat().st_size, after_bytes=db_path.stat().st_size)
        return report
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/novelagent.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")
    print(json.dumps(cleanup(args.db, apply=args.apply, vacuum=args.vacuum), ensure_ascii=False, indent=2))
