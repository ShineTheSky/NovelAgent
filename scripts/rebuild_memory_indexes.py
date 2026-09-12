"""Build per-project memory.md sliding-window snapshots."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.memory_manager import MemoryManager


def main() -> None:
    parser = argparse.ArgumentParser(description="重建项目记忆快照")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--project", action="append", default=[])
    args = parser.parse_args()
    selected = set(args.project)
    global_dir = args.workspace.parent / "global_memory"
    for project in sorted(path for path in args.workspace.iterdir() if path.is_dir()):
        if selected and project.name not in selected:
            continue
        if not (project / ".memory").is_dir():
            continue
        index = MemoryManager(str(project), global_memory_dir=str(global_dir)).rebuild_index()
        count = sum(bool(re.match(r"\| \d+ \|", line)) for line in index.splitlines())
        print(f"{project.name}: {count} 条快照条目")


if __name__ == "__main__":
    main()
