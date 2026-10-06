"""Back up and supplement one project's existing History; dry-run by default."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.evidence_repair import repair_history_evidence
from novelagent.history.order_repair import repair_history_order


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-db", type=Path, default=root / "data/novelagent.db")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    project = (root / "workspace" / args.project_id).resolve()
    if project.parent != (root / "workspace").resolve() or not project.is_dir():
        parser.error("--project-id must identify one workspace project")
    result = repair_history_evidence(args.trace_db, project, apply=args.apply)
    if args.apply:
        result["order"] = repair_history_order(args.trace_db, project, apply=True)
        result["verification"] = repair_history_evidence(args.trace_db, project)
        if result["verification"]["events_added"]:
            raise RuntimeError("History evidence repair was not idempotent")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
