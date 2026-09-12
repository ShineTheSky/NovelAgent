"""Move project files into the canonical layout without deleting or overwriting content."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PROJECT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", re.IGNORECASE)
VOLUME_OUTLINE = re.compile(r"^outline_(\d+)\.0\.0\.md$", re.IGNORECASE)
CHAPTER_OUTLINE = re.compile(r"^outline_(\d+)\.(\d+)\.0\.md$", re.IGNORECASE)
LEGACY_CHAPTER = re.compile(r"^ch(\d+)\.md$", re.IGNORECASE)
DEEP_VOLUME = re.compile(r"^outlines/volume_(\d{3})\.md$", re.IGNORECASE)
DEEP_CHAPTER = re.compile(r"^outlines/volume_(\d{3})/chapter_(\d{3})\.md$", re.IGNORECASE)
DEEP_SECTION = re.compile(r"^manuscript/volume_(\d{3})/chapter_(\d{3})/section_(\d{3})\.md$", re.IGNORECASE)


def add_move(moves: list[tuple[Path, Path]], source: Path, destination: Path) -> None:
    if source == destination:
        return
    if destination.exists() or any(planned == destination for _, planned in moves):
        raise RuntimeError(f"目标已存在，停止以防覆盖：{destination}")
    moves.append((source, destination))


def has_target(moves: list[tuple[Path, Path]], target: Path) -> bool:
    return target.exists() or any(destination == target for _, destination in moves)


def project_moves(project_dir: Path) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []

    for source in sorted(project_dir.glob("outlines/**/*.md")):
        relative = source.relative_to(project_dir).as_posix()
        if match := DEEP_VOLUME.match(relative):
            add_move(moves, source, project_dir / "outlines" / f"outline_{int(match.group(1))}.0.0.md")
        elif match := DEEP_CHAPTER.match(relative):
            add_move(moves, source, project_dir / "outlines" / f"outline_{int(match.group(1))}.{int(match.group(2))}.0.md")

    for source in sorted(project_dir.glob("manuscript/**/*.md")):
        match = DEEP_SECTION.match(source.relative_to(project_dir).as_posix())
        if match:
            add_move(moves, source, project_dir / "chapters" / "content_{}.{}.{}.md".format(*(int(value) for value in match.groups())))

    for category in ("world", "characters", "reference"):
        source_directory = project_dir / "materials" / category
        if source_directory.is_dir():
            for source in sorted(source_directory.rglob("*")):
                if source.is_file():
                    add_move(moves, source, project_dir / category / source.relative_to(source_directory))

    for source in sorted(project_dir.glob("outline_*.md")):
        volume = VOLUME_OUTLINE.match(source.name)
        chapter = CHAPTER_OUTLINE.match(source.name)
        if volume:
            add_move(moves, source, project_dir / "outlines" / f"outline_{int(volume.group(1))}.0.0.md")
        elif chapter:
            add_move(moves, source, project_dir / "outlines" / f"outline_{int(chapter.group(1))}.{int(chapter.group(2))}.0.md")

    legacy_outline = project_dir / "outline.md"
    if legacy_outline.is_file():
        volume_one = project_dir / "outlines/outline_1.0.0.md"
        target = project_dir / ("archive/legacy/outline.md" if has_target(moves, volume_one) else "outlines/outline_1.0.0.md")
        add_move(moves, legacy_outline, target)

    chapters_dir = project_dir / "chapters"
    if chapters_dir.is_dir():
        unnamed: list[Path] = []
        for source in sorted(chapters_dir.glob("*.md"), key=lambda item: item.name.lower()):
            match = LEGACY_CHAPTER.match(source.name)
            if match:
                add_move(moves, source, project_dir / "chapters" / f"content_1.{int(match.group(1))}.1.md")
            elif not source.name.startswith("content_"):
                unnamed.append(source)
        chapter_number = 1
        for source in unnamed:
            while has_target(moves, project_dir / "chapters" / f"content_1.{chapter_number}.1.md"):
                chapter_number += 1
            add_move(moves, source, project_dir / "chapters" / f"content_1.{chapter_number}.1.md")
            chapter_number += 1

    for old_directory, target_directory in (
        ("world", "world"),
        ("characters", "characters"),
        ("project/characters", "characters"),
        ("reference", "reference"),
    ):
        source_directory = project_dir / old_directory
        if source_directory.is_dir():
            for source in sorted(source_directory.rglob("*")):
                if source.is_file():
                    add_move(moves, source, project_dir / target_directory / source.relative_to(source_directory))

    legacy_memory = project_dir / "memory.md"
    if legacy_memory.is_file():
        add_move(moves, legacy_memory, project_dir / "archive/legacy/memory.md")
    return moves


def write_manifest(project_dir: Path, moves: list[tuple[Path, Path]]) -> None:
    if not moves:
        return
    manifest = project_dir / "archive/legacy/migration_manifest.md"
    lines = ["# 存储结构迁移记录", "", "以下文件仅作移动或重命名，内容未删除：", ""]
    lines.extend(
        f"- `{source.relative_to(project_dir).as_posix()}` → `{destination.relative_to(project_dir).as_posix()}`"
        for source, destination in moves
    )
    if manifest.exists():
        existing = manifest.read_text(encoding="utf-8")
        additions = [line for line in lines[4:] if line not in existing]
        if additions:
            manifest.write_text(existing.rstrip() + "\n" + "\n".join(additions) + "\n", encoding="utf-8")
        return
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def migrate(project_dir: Path, apply: bool) -> list[tuple[Path, Path]]:
    moves = project_moves(project_dir)
    if apply:
        write_manifest(project_dir, moves)
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
    return moves


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--apply", action="store_true", help="perform moves after planning")
    parser.add_argument("--exclude", action="append", default=[], help="project UUID to leave unchanged; may be repeated")
    args = parser.parse_args()

    projects = sorted(
        path for path in args.workspace.iterdir()
        if path.is_dir() and PROJECT_ID.match(path.name) and path.name not in args.exclude
    )
    for project_dir in projects:
        moves = migrate(project_dir, args.apply)
        print(f"{project_dir.name}: {len(moves)} file(s)")
        for source, destination in moves:
            print(f"  {source.relative_to(project_dir)} -> {destination.relative_to(project_dir)}")


if __name__ == "__main__":
    main()
