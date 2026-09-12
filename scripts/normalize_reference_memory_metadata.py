"""Ensure reference bindings carry durable weight and timestamps."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.file_store import FileStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    for project in sorted(path for path in args.workspace.iterdir() if path.is_dir()):
        references = project / ".memory" / "references"
        if not references.is_dir():
            continue
        store = FileStore(str(project))
        for source in references.rglob("*.md"):
            rel = str(source.relative_to(project / ".memory")).replace("\\", "/")
            store.write(rel, store.read(rel))
            print(f"{project.name}: {rel}")


if __name__ == "__main__":
    main()
