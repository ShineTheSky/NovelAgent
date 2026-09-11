"""按卷、章、小节读取项目小说文件的 API。"""

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")

VOLUME_OUTLINE_PATTERN = re.compile(r"^outline_(\d+)\.0\.0\.md$")
CHAPTER_OUTLINE_PATTERN = re.compile(r"^outline_(\d+)\.(\d+)\.0\.md$")
SECTION_PATTERN = re.compile(r"^content_(\d+)\.(\d+)\.(\d+)\.md$")
LEGACY_CHAPTER_PATTERN = re.compile(r"^ch(\d+)\.md$", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _project_dir(project_id: str, request: Request) -> Path:
    project_dir = Path(request.app.state.config.get("working_dir", "./workspace")) / project_id
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="项目目录不存在")
    return project_dir


def _fallback_title(kind: str, volume: int, chapter: int = 0, section: int = 0) -> str:
    if kind == "volume_outline":
        return f"第 {volume} 卷卷纲"
    if kind == "chapter_outline":
        return f"第 {volume} 卷第 {chapter} 章章纲"
    return f"第 {volume} 卷第 {chapter} 章第 {section} 节"


def _document(project_dir: Path, path: Path, kind: str, volume: int, chapter: int = 0, section: int = 0) -> dict:
    content = path.read_text(encoding="utf-8")
    heading = HEADING_PATTERN.search(content)
    stat = path.stat()
    return {
        "id": f"{kind}:{volume}.{chapter}.{section}",
        "type": kind,
        "volume": volume,
        "chapter": chapter or None,
        "section": section or None,
        "title": heading.group(1) if heading else _fallback_title(kind, volume, chapter, section),
        "path": path.relative_to(project_dir).as_posix(),
        "char_count": len(re.sub(r"\s+", "", content)),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _build_novel_tree(project_dir: Path) -> tuple[dict, dict[str, tuple[dict, Path]]]:
    volumes: dict[int, dict] = {}
    documents: dict[str, tuple[dict, Path]] = {}

    def ensure_volume(number: int) -> dict:
        return volumes.setdefault(number, {"volume": number, "title": f"第 {number} 卷", "outline": None, "chapters": {}})

    def ensure_chapter(volume: int, number: int) -> dict:
        volume_node = ensure_volume(volume)
        return volume_node["chapters"].setdefault(number, {
            "volume": volume,
            "chapter": number,
            "title": f"第 {volume} 卷第 {number} 章",
            "outline": None,
            "sections": [],
        })

    for path in project_dir.glob("outline_*.md"):
        volume_match = VOLUME_OUTLINE_PATTERN.match(path.name)
        chapter_match = CHAPTER_OUTLINE_PATTERN.match(path.name)
        if volume_match:
            volume = int(volume_match.group(1))
            document = _document(project_dir, path, "volume_outline", volume)
            node = ensure_volume(volume)
            node["outline"] = document
            node["title"] = document["title"]
            documents[document["id"]] = (document, path)
        elif chapter_match:
            volume, chapter = (int(value) for value in chapter_match.groups())
            document = _document(project_dir, path, "chapter_outline", volume, chapter)
            node = ensure_chapter(volume, chapter)
            node["outline"] = document
            node["title"] = document["title"]
            documents[document["id"]] = (document, path)

    legacy_outline = project_dir / "outline.md"
    if legacy_outline.exists() and not ensure_volume(1)["outline"]:
        document = _document(project_dir, legacy_outline, "volume_outline", 1)
        volume = ensure_volume(1)
        volume["outline"] = document
        volume["title"] = document["title"]
        documents[document["id"]] = (document, legacy_outline)

    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        for path in chapters_dir.glob("content_*.md"):
            match = SECTION_PATTERN.match(path.name)
            if not match:
                continue
            volume, chapter, section = (int(value) for value in match.groups())
            document = _document(project_dir, path, "section", volume, chapter, section)
            ensure_chapter(volume, chapter)["sections"].append(document)
            documents[document["id"]] = (document, path)

        for path in chapters_dir.glob("ch*.md"):
            match = LEGACY_CHAPTER_PATTERN.match(path.name)
            if not match:
                continue
            chapter = int(match.group(1))
            node = ensure_chapter(1, chapter)
            # 新版 content_1.X.1.md 优先；旧版 chXX.md 仅用于兼容已有项目。
            if node["sections"]:
                continue
            document = _document(project_dir, path, "section", 1, chapter, 1)
            node["title"] = document["title"]
            node["sections"].append(document)
            documents[document["id"]] = (document, path)

    result = []
    for volume in sorted(volumes.values(), key=lambda item: item["volume"]):
        chapters = []
        for chapter in sorted(volume["chapters"].values(), key=lambda item: item["chapter"]):
            chapter["sections"].sort(key=lambda item: item["section"])
            chapter["char_count"] = sum(item["char_count"] for item in chapter["sections"]) + (chapter["outline"] or {}).get("char_count", 0)
            chapters.append(chapter)
        volume["chapters"] = chapters
        volume["char_count"] = sum(item["char_count"] for item in chapters) + (volume["outline"] or {}).get("char_count", 0)
        result.append(volume)

    return {"volumes": result}, documents


@router.get("/projects/{project_id}/novel")
async def get_novel_tree(project_id: str, request: Request):
    """读取项目的卷纲、章纲和小节，并按数字编号排序。"""
    project_dir = _project_dir(project_id, request)
    tree, _ = _build_novel_tree(project_dir)
    return {"project_id": project_id, **tree}


@router.get("/projects/{project_id}/novel/nodes/{node_id}")
async def get_novel_node(project_id: str, node_id: str, request: Request):
    """读取小说树中一个已索引节点的完整 Markdown 正文。"""
    project_dir = _project_dir(project_id, request)
    _, documents = _build_novel_tree(project_dir)
    item = documents.get(node_id)
    if item is None:
        raise HTTPException(status_code=404, detail="小说节点不存在")
    document, path = item
    return {"node": document, "content": path.read_text(encoding="utf-8")}
