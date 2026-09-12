"""Rebuild structured global user memory from archived legacy user records."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrate_memory_layout import GLOBAL_USER_MARKERS
from reclassify_project_memory import split_frontmatter, summary_for


def is_global(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    return any(marker in text for marker in GLOBAL_USER_MARKERS)


def collect(workspace: Path) -> list[dict]:
    entries = []
    seen: set[str] = set()
    for project in sorted(path for path in workspace.iterdir() if path.is_dir()):
        source_dir = project / ".memory" / "archive" / "legacy" / "user"
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.rglob("*.md")):
            meta, body = split_frontmatter(source.read_text(encoding="utf-8"))
            title = str(meta.get("name") or source.stem).strip()
            if not is_global(title, body):
                continue
            fingerprint = f"{title}\n{body}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            tags = meta.get("tags", [])
            timestamp = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            entries.append({
                "title": title,
                "body": body,
                "tags": tags if isinstance(tags, list) else [],
                "summary": str(meta.get("summary") or summary_for(body, title)),
                "source": f"{project.name}/user/{source.name}",
                "created": str(meta.get("created") or timestamp),
                "updated": str(meta.get("updated") or timestamp),
                "weight": meta.get("weight", 60),
            })
    return entries


def render(entries: list[dict]) -> str:
    lines = [
        "---", "type: global_user_collection", "tags: []", "summary: 跨项目用户偏好",
        "status: active", "migration: typed_global_user_v2", "---", "", "# 全局用户偏好", "",
    ]
    for index, entry in enumerate(entries, 1):
        metadata = {
            "id": f"entry-{index:03d}", "tags": entry["tags"], "summary": entry["summary"],
            "source": entry["source"], "created": entry["created"], "updated": entry["updated"],
            "weight": entry["weight"],
        }
        lines.extend([
            f"## [user] {entry['title']}", "<!-- memory-entry",
            yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip(), "-->", "", entry["body"], "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="重建结构化全局用户偏好")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    entries = collect(args.workspace)
    print(f"全局用户偏好: {len(entries)} 条")
    if not args.apply:
        return
    target = args.workspace.parent / "global_memory" / "user.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name("user-before-reclassification.md")
        number = 2
        while backup.exists():
            backup = target.with_name(f"user-before-reclassification-{number}.md")
            number += 1
        target.replace(backup)
    target.write_text(render(entries), encoding="utf-8")


if __name__ == "__main__":
    main()
