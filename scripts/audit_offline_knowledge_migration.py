"""Audit legacy knowledge and optionally verify an isolated temporary snapshot.

Never starts the backend, loads model routing, requests embeddings, or edits a project.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.offline_migration import offline_plan, offline_snapshot, verify_snapshot_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--project", type=Path, action="append", help="Audit only this project (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Copy into an automatically removed temporary directory and verify")
    parser.add_argument("--trace-database", type=Path, help="Optional read-only original Trace database; queries only referenced event IDs")
    parser.add_argument("--verify-runtime", action="store_true", help="Dry-run only: retrieval/context/scoring with existing saved vectors, no embedding model")
    parser.add_argument("--repair-history", action="store_true", help="Dry-run only: attach exact tool/dispatch Trace sources in copied History")
    args = parser.parse_args()
    if args.verify_runtime and not args.dry_run:
        parser.error("--verify-runtime requires --dry-run")
    if args.repair_history and not (args.dry_run and args.trace_database):
        parser.error("--repair-history requires --dry-run and --trace-database")
    projects = args.project or sorted(p for p in args.workspace.iterdir() if p.is_dir() and not p.name.startswith("."))
    reports = []
    for project in projects:
        if args.dry_run:
            with tempfile.TemporaryDirectory(prefix="novelagent-offline-migration-") as temp:
                report = offline_snapshot(project, Path(temp) / "project", args.trace_database, repair_history=args.repair_history)
                if args.verify_runtime:
                    report["runtime_verification"] = asyncio.run(verify_snapshot_runtime(Path(temp) / "project"))
        else:
            report = offline_plan(project, args.trace_database)
        reports.append({"project": str(project.resolve()), "counts": report["counts"],
            "integrity_issues": report["integrity_issues"], "snapshot_verified": report.get("snapshot_verified", False),
            "source_unchanged": report.get("source_unchanged", None),
            "runtime_verification": report.get("runtime_verification"),
            "history_repair": report.get("history_repair"),
            "legacy_review": [{k: row[k] for k in ("legacy_id", "path", "status", "missing_history_ids", "missing_event_ids", "mismatched_excerpt_ids", "exact_trace_history_mappings", "trace_unmapped_types", "reason")}
                              for row in report["legacy"] if row["status"] != "evidence_staged"],
            "structured_review": [{k: row[k] for k in ("id", "version", "layer", "published", "issues")}
                                  for row in report["structured"] if row["issues"]]})
    print(json.dumps({"mode": "offline_no_models", "temporary_snapshots_removed": args.dry_run, "projects": reports},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
