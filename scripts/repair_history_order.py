"""Recover old History execution order. Dry-run by default; --apply backs up first."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.order_repair import repair_history_order


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument("--trace-db", type=Path, default=root / "data/novelagent.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    project = (workspace / args.project_id).resolve()
    if project == workspace or not project.is_relative_to(workspace) or not project.is_dir():
        parser.error("Project must be an existing directory inside the workspace")
    backup = None
    if args.apply:
        source_db = project / ".history/history.sqlite3"
        backup = project / ".history/migration_backups" / datetime.now(timezone.utc).strftime("order-%Y%m%dT%H%M%S.%fZ.sqlite3")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
    report = repair_history_order(args.trace_db, project, apply=args.apply)
    report["backup"] = str(backup) if backup else None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
