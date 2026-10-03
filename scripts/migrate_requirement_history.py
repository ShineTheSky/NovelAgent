"""Separate shared requirements from file execution evidence; dry-run by default."""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.memory_links import link_memories, migrate_requirements


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
        backup = project / ".history/migration_backups" / datetime.now(timezone.utc).strftime("requirements-%Y%m%dT%H%M%S.%fZ")
        backup.mkdir(parents=True)
        source_db = project / ".history/history.sqlite3"
        with sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True) as source, sqlite3.connect(backup / "history.sqlite3") as target:
            source.backup(target)
        for path in (project / ".memory").glob("*/mem_*.md"):
            target = backup / path.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    result = migrate_requirements(args.trace_db, project, apply=args.apply)
    if args.apply:
        result["memories"] = link_memories(args.trace_db, project, apply=True, replace_legacy_links=True)
        result["backup"] = str(backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
