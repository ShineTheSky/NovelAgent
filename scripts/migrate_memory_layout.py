"""Migrate legacy project memories to the three-location memory layout.

The script never deletes a legacy record.  It folds project-scoped records into
``.memory/project_rules.md`` and moves the source files to
``.memory/archive/legacy/`` so every merged section has recoverable evidence.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager


LEGACY_GROUPS = ("user", "feedback", "project", "workflow", "review")
GLOBAL_USER_MARKERS = (
    "subagent", "agent", "attachments", "reviewer", "工作方式", "流程偏好", "提示词", "askuserquestion",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _is_global_user(path: Path, content: str) -> bool:
    """Only promote cross-project collaboration preferences from old user files."""
    if path.parent.name != "user":
        return False
    text = f"{path.stem}\n{content}".lower()
    return any(marker in text for marker in GLOBAL_USER_MARKERS)


def _section(path: Path, content: str) -> str:
    return f"## {path.stem}\n\n> 迁移来源：`{path.as_posix()}`\n\n{content}\n"


def _archived_global_sections(memory_dir: Path) -> list[str]:
    """Recover global entries when a prior project migration completed first."""
    archived_user = memory_dir / "archive" / "legacy" / "user"
    if not archived_user.is_dir():
        return []
    sections = []
    for source in sorted(archived_user.rglob("*.md")):
        content = _read(source)
        if _is_global_user(source, content):
            sections.append(_section(Path("user") / source.name, content))
    return sections


def _write_aggregate(path: Path, title: str, memory_type: str, sections: list[str], *, append: bool = False) -> None:
    if not sections:
        return
    if path.exists():
        if not append:
            raise RuntimeError(f"规范目标已存在，拒绝覆盖：{path}")
        existing = _read(path)
        path.write_text(existing + "\n\n" + "\n".join(sections), encoding="utf-8")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "---\n"
        f"type: {memory_type}\n"
        "tags: []\n"
        f"summary: {title}\n"
        "status: active\n"
        "migration: legacy_memory_layout_v1\n"
        "---\n\n"
        f"# {title}\n\n"
        "> 以下内容由旧记忆迁移而来；每段都保留原始来源，原文件位于 archive/legacy。\n\n"
    )
    path.write_text(header + "\n".join(sections), encoding="utf-8")


def _archive_target(memory_dir: Path, source: Path) -> Path:
    return memory_dir / "archive" / "legacy" / source.parent.name / source.name


def _move_to_archive(memory_dir: Path, sources: list[Path]) -> None:
    for source in sources:
        target = _archive_target(memory_dir, source)
        if target.exists():
            raise RuntimeError(f"归档目标已存在，拒绝覆盖：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def archive_remaining_legacy(memory_dir: Path) -> int:
    """Resume a partial migration without rewriting its canonical files."""
    sources: list[Path] = []
    for group in LEGACY_GROUPS:
        group_dir = memory_dir / group
        if group_dir.is_dir():
            sources.extend(sorted(group_dir.rglob("*.md")))
    user_prefs = memory_dir / "user_prefs.md"
    if user_prefs.exists():
        sources.append(user_prefs)
    moved = 0
    for source in sources:
        target = _archive_target(memory_dir, source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if source.read_bytes() != target.read_bytes():
                raise RuntimeError(f"归档目标内容不一致，拒绝覆盖：{target}")
            source.unlink()
        else:
            shutil.move(str(source), str(target))
        moved += 1
    return moved


def _wrap_reference(source: Path) -> None:
    content = _read(source)
    if content.startswith("---"):
        return
    source.write_text(
        "---\n"
        "type: reference_binding\n"
        "tags: []\n"
        f"summary: {source.stem}\n"
        "status: active\n"
        "---\n\n"
        f"# {source.stem}\n\n"
        f"{content}\n",
        encoding="utf-8",
    )


def migrate_project(project_dir: Path, apply: bool) -> tuple[dict[str, int], list[str]]:
    memory_dir = project_dir / ".memory"
    if not memory_dir.is_dir():
        return {"project_rules": 0, "global_user": 0, "references": 0}, []

    legacy: list[Path] = []
    for group in LEGACY_GROUPS:
        group_dir = memory_dir / group
        if group_dir.is_dir():
            legacy.extend(sorted(group_dir.rglob("*.md")))
    user_prefs = memory_dir / "user_prefs.md"
    if user_prefs.exists():
        legacy.append(user_prefs)

    global_sections: list[str] = []
    project_sections: list[str] = []
    for source in legacy:
        content = _read(source)
        if not content:
            continue
        if _is_global_user(source, content):
            global_sections.append(_section(source.relative_to(memory_dir), content))
        else:
            project_sections.append(_section(source.relative_to(memory_dir), content))
    if not global_sections:
        global_sections = _archived_global_sections(memory_dir)

    old_references = memory_dir / "reference"
    reference_sources = sorted(old_references.rglob("*.md")) if old_references.is_dir() else []
    old_index = memory_dir / "memory.md"

    result = {
        "project_rules": len(project_sections),
        "global_user": len(global_sections),
        "references": len(reference_sources),
    }
    if not apply:
        return result, global_sections

    _write_aggregate(memory_dir / "project_rules.md", "项目规则与修正", "project_rules", project_sections)

    references_dir = memory_dir / "references"
    for source in reference_sources:
        target = references_dir / source.relative_to(old_references)
        if target.exists():
            raise RuntimeError(f"参考目标已存在，拒绝覆盖：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        _wrap_reference(target)
    if old_references.is_dir() and not any(old_references.iterdir()):
        old_references.rmdir()

    _move_to_archive(memory_dir, legacy)
    if old_index.exists():
        index_target = memory_dir / "archive" / "legacy" / "memory.md"
        if not index_target.exists():
            index_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_index), str(index_target))
    IndexManager(FileStore(str(project_dir))).rebuild(
        ("references/", "archive/", "user/", "feedback/", "project/", "workflow/", "review/", "user_prefs.md")
    )
    return result, global_sections


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移旧 .memory 到三层记忆布局")
    parser.add_argument("workspace", type=Path, help="workspace 根目录")
    parser.add_argument("--apply", action="store_true", help="执行迁移；默认仅预览")
    parser.add_argument("--archive-only", action="store_true", help="仅归档此前部分迁移后残留的旧文件")
    parser.add_argument("--project", action="append", default=[], help="仅迁移指定项目 ID，可重复")
    args = parser.parse_args()

    selected = set(args.project)
    totals = {"project_rules": 0, "global_user": 0, "references": 0}
    all_global_sections: list[str] = []
    for project_dir in sorted(path for path in args.workspace.iterdir() if path.is_dir()):
        if selected and project_dir.name not in selected:
            continue
        if args.archive_only:
            moved = archive_remaining_legacy(project_dir / ".memory") if (project_dir / ".memory").is_dir() else 0
            if moved:
                print(f"{project_dir.name}: 归档旧文件 {moved}")
            continue
        result, global_sections = migrate_project(project_dir, args.apply)
        if any(result.values()):
            print(f"{project_dir.name}: 项目规则 {result['project_rules']}，全局偏好 {result['global_user']}，参考绑定 {result['references']}")
        for key, value in result.items():
            totals[key] += value
        all_global_sections.extend(global_sections)
    if args.apply:
        _write_aggregate(
            args.workspace.parent / "global_memory" / "user.md",
            "全局用户偏好",
            "global_user",
            all_global_sections,
            append=True,
        )
    print(f"合计: 项目规则 {totals['project_rules']}，全局偏好 {totals['global_user']}，参考绑定 {totals['references']}")


if __name__ == "__main__":
    main()
