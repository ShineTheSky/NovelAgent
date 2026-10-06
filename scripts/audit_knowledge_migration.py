"""Read-only legacy migration inventory/plan. No LLM calls or data changes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.migration import audit, preview


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--plan", action="store_true", help="Include eligible roots and resumable progress")
    args = parser.parse_args()
    print(json.dumps((preview if args.plan else audit)(args.project_dir), ensure_ascii=False, indent=2))
