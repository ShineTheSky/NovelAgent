"""Link existing project Insight/Memory/Pattern to History; dry-run, no LLM calls."""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.memory_links import link_records


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-db", type=Path, default=root / "data/novelagent.db")
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--layers", nargs="+", choices=("insight", "memory", "pattern"), default=["memory"])
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    project = (workspace / args.project_id).resolve()
    if project == workspace or not project.is_relative_to(workspace) or not project.is_dir():
        parser.error("Project must be an existing directory inside the workspace")
    backup = None
    if args.apply:
        backup = project / ".history/migration_backups" / datetime.now(timezone.utc).strftime("record-links-%Y%m%dT%H%M%S.%fZ")
        backup.mkdir(parents=True)
        for layer in args.layers:
            for source in (project / f".{layer}").glob("*/*.md"):
                target = backup / source.relative_to(project)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    result = link_records(args.trace_db, project, layers=tuple(dict.fromkeys(args.layers)), apply=args.apply)
    result["backup"] = str(backup) if backup else None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
