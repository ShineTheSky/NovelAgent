"""Dry-run by default; --apply backs up and normalizes one project's History."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.history.process_repair import repair_process_ownership


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair_process_ownership(args.project, apply=args.apply), ensure_ascii=False, indent=2))
