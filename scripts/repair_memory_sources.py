"""Repair reviewed legacy memory provenance; dry-run by default, no LLM calls."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.source_repair import repair_sources


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--trace-db", type=Path, default=root / "data/novelagent.db")
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    workspace = args.workspace.resolve()
    project = (workspace / plan["project_id"]).resolve()
    if project == workspace or not project.is_relative_to(workspace) or not project.is_dir():
        parser.error("Project must be an existing directory inside the workspace")
    print(json.dumps(repair_sources(args.trace_db, project, plan, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
