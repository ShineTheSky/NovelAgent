"""Rebuild migrated project memory into typed entries and a fresh memory.md index."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager
from migrate_memory_layout import GLOBAL_USER_MARKERS


PROJECT_GROUPS = ("user", "feedback", "project", "workflow", "review")


def split_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content.strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta if isinstance(meta, dict) else {}, parts[2].strip()


def is_global_user(group: str, title: str, body: str) -> bool:
    if group != "user":
        return False
    text = f"{title}\n{body}".lower()
    return any(marker in text for marker in GLOBAL_USER_MARKERS)


def classify(group: str, title: str, body: str) -> str:
    text = f"{title}\n{body}".lower()
    if group == "feedback":
        return "correction"
    if group == "workflow":
        return "rule"
    if group == "review":
        return "correction"
    if group == "project":
        if any(word in text for word in ("规则", "规范", "必须", "禁止", "流程", "调用")):
            return "rule"
        if any(word in text for word in ("修改", "变更", "调整", "确认", "要求")):
            return "decision"
        return "context"
    if any(word in text for word in ("要求", "偏好", "选择", "确认", "必须", "不要")):
        return "decision"
    return "context"


def summary_for(body: str, fallback: str) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    compact = re.sub(r"^#+\s*", "", compact)
    return (compact[:120] or fallback).strip()


def default_weight(kind: str) -> int:
    return {"rule": 80, "correction": 75, "decision": 65, "context": 50}[kind]


def collect_entries(memory_dir: Path) -> list[dict]:
    entries = []
    source_roots: list[tuple[Path, bool]] = []
    for group in PROJECT_GROUPS:
        live = memory_dir / group
        archive = memory_dir / "archive" / "legacy" / group
        if live.is_dir() and any(live.rglob("*.md")):
            source_roots.append((live, False))
        elif archive.is_dir():
            source_roots.append((archive, True))
    live_user_prefs = memory_dir / "user_prefs.md"
    archived_user_prefs = memory_dir / "archive" / "legacy" / ".memory" / "user_prefs.md"
    if live_user_prefs.exists():
        source_roots.append((live_user_prefs, False))
    elif archived_user_prefs.exists():
        source_roots.append((archived_user_prefs, True))

    for root, is_archived in source_roots:
        sources = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for source in sources:
            group = "user_prefs" if source.name == "user_prefs.md" else root.name
            raw = source.read_text(encoding="utf-8")
            meta, body = split_frontmatter(raw)
            title = str(meta.get("name") or source.stem).strip()
            if is_global_user(group, title, body):
                continue
            tags = meta.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            original = f"{group}/{source.name}" if is_archived else str(source.relative_to(memory_dir)).replace("\\", "/")
            timestamp = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            kind = classify(group, title, body)
            entries.append({
                "kind": kind,
                "title": title,
                "tags": [str(tag) for tag in tags],
                "summary": str(meta.get("summary") or summary_for(body, title)),
                "source": original,
                "created": str(meta.get("created") or timestamp),
                "updated": str(meta.get("updated") or timestamp),
                "weight": meta.get("weight", default_weight(kind)),
                "body": body,
            })
    return entries


def render_project_rules(entries: list[dict]) -> str:
    lines = [
        "---",
        "type: project_memory",
        "tags: []",
        "summary: 当前项目的长期记忆",
        "status: active",
        "migration: typed_project_memory_v2",
        "---",
        "",
        "# 项目记忆",
        "",
        "> 条目按 rule、correction、decision、context 分类；原始旧文件保留在 archive/legacy。",
        "",
    ]
    for index, entry in enumerate(entries, 1):
        entry_id = f"entry-{index:03d}"
        metadata = {
            "id": entry_id,
            "tags": entry["tags"],
            "summary": entry["summary"],
            "source": entry["source"],
            "created": entry["created"],
            "updated": entry["updated"],
            "weight": entry["weight"],
        }
        meta_yaml = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        lines.extend([
            f"## [{entry['kind']}] {entry['title']}",
            "<!-- memory-entry",
            meta_yaml,
            "-->",
            "",
            entry["body"],
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def move_as_unique_backup(source: Path, archive_dir: Path, label: str) -> None:
    target = archive_dir / label
    number = 2
    while target.exists():
        target = archive_dir / f"{Path(label).stem}-{number}{Path(label).suffix}"
        number += 1
    shutil.move(str(source), str(target))


def rebuild_project(project_dir: Path, apply: bool) -> dict[str, int]:
    memory_dir = project_dir / ".memory"
    if not memory_dir.is_dir():
        return {}
    entries = collect_entries(memory_dir)
    counts = {kind: sum(entry["kind"] == kind for entry in entries) for kind in ("rule", "correction", "decision", "context")}
    if not apply or not entries:
        return counts

    archive_dir = memory_dir / "archive" / "legacy"
    archive_dir.mkdir(parents=True, exist_ok=True)
    rules_path = memory_dir / "project_rules.md"
    if rules_path.exists():
        move_as_unique_backup(rules_path, archive_dir, "project_rules-before-reclassification.md")
    index_path = memory_dir / "memory.md"
    if index_path.exists():
        move_as_unique_backup(index_path, archive_dir, "memory-before-reclassification.md")
    rules_path.write_text(render_project_rules(entries), encoding="utf-8")
    IndexManager(FileStore(str(project_dir))).rebuild(
        ("references/", "archive/", "user/", "feedback/", "project/", "workflow/", "review/", "user_prefs.md")
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="重分类项目旧记忆并重建 memory.md")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--project", action="append", default=[])
    args = parser.parse_args()
    selected = set(args.project)
    for project_dir in sorted(path for path in args.workspace.iterdir() if path.is_dir()):
        if selected and project_dir.name not in selected:
            continue
        counts = rebuild_project(project_dir, args.apply)
        if counts:
            print(f"{project_dir.name}: " + ", ".join(f"{kind}={count}" for kind, count in counts.items()))


if __name__ == "__main__":
    main()
