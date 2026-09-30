"""Import legacy Trace evidence into project History; dry-run by default."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.backfill import backfill, rebuild_project_history, reorganize


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-db", type=Path, default=root / "data/novelagent.db")
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--apply", action="store_true", help="Write History; otherwise report only")
    parser.add_argument("--check", action="store_true", help="Read-only validation of already imported History")
    parser.add_argument("--repair", action="store_true", help="Append missing evidence to deferred imports without changing revisions")
    parser.add_argument("--reorganize", action="store_true", help="Replace old file aggregates with one History per Agent run (backup kept only on failure)")
    parser.add_argument("--rebuild", action="store_true", help="Replace a project's old Trace-derived History with turn-correct units")
    args = parser.parse_args()
    if args.rebuild and not args.project_id:
        parser.error("--rebuild requires --project-id")
    if (args.reorganize or args.rebuild) and (args.check or args.repair):
        parser.error("--reorganize/--rebuild cannot be combined with --check or --repair")
    if args.reorganize and args.rebuild:
        parser.error("--reorganize and --rebuild are mutually exclusive")
    if sum((args.apply, args.check, args.repair)) > 1:
        parser.error("--apply, --check and --repair are mutually exclusive")
    if args.rebuild:
        result = rebuild_project_history(args.trace_db, args.workspace, args.project_id,
                                         apply=args.apply)
    elif args.reorganize:
        result = reorganize(args.trace_db, args.workspace, apply=args.apply,
                            project_id=args.project_id)
    else:
        result = backfill(args.trace_db, args.workspace, apply=args.apply, check=args.check,
                          repair=args.repair, project_id=args.project_id)
        if args.apply and not result["errors"]:
            result["reorganization"] = reorganize(
                args.trace_db, args.workspace, apply=True, project_id=args.project_id,
            )
            result["errors"].extend(result["reorganization"]["errors"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
