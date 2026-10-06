"""Validate and replay one completed retain Trace into a disposable snapshot."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.offline_trace_replay import ReplayRejected, replay_completed_retain_trace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Project workspace directory")
    parser.add_argument("--trace-database", type=Path, required=True, help="Read-only Trace SQLite database")
    parser.add_argument("--trace-id", required=True, help="Completed knowledge:retain Trace ID")
    parser.add_argument("--output", type=Path, help="Optional new JSON report path; existing files are not overwritten")
    args = parser.parse_args(argv)
    try:
        project = args.project.resolve()
        trace_database = args.trace_database.resolve()
        report_path = args.output.resolve() if args.output else None
        if report_path and (report_path == trace_database or report_path == project or project in report_path.parents):
            print(json.dumps({"status": "rejected", "reason_code": "report_path_overlaps_source"}, ensure_ascii=False))
            return 2
        report = replay_completed_retain_trace(project, trace_database, args.trace_id)
    except ReplayRejected as exc:
        report = {"mode": "offline_completed_retain_replay", "status": "rejected",
                  "trace_id": args.trace_id, "reason_code": exc.code, "details": exc.details,
                  "application_llm_calls": 0, "embedding_calls": 0}
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        # Keep reports free of original prompts, model output, and SQL values.
        report = {"mode": "offline_completed_retain_replay", "status": "failed",
                  "trace_id": args.trace_id, "reason_code": type(exc).__name__.lower(),
                  "application_llm_calls": 0, "embedding_calls": 0}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        try:
            with args.output.resolve().open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered + "\n")
        except FileExistsError:
            print(json.dumps({"status": "rejected", "reason_code": "report_file_exists"}, ensure_ascii=False))
            return 2
    else:
        print(rendered)
    return 0 if report.get("status") == "replayed_in_temporary_snapshot" else 1


if __name__ == "__main__":
    raise SystemExit(main())
