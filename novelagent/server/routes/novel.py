"""按卷、章、小节读取项目小说文件的 API。"""

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")

VOLUME_OUTLINE_PATTERN = re.compile(r"^outlines/outline_(\d+)\.0\.0\.md$")
CHAPTER_OUTLINE_PATTERN = re.compile(r"^outlines/outline_(\d+)\.(\d+)\.0\.md$")
SECTION_PATTERN = re.compile(r"^chapters/content_(\d+)\.(\d+)\.(\d+)\.md$")
HEADING_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
METADATA_TITLE_PATTERN = re.compile(r"^\|\s*\*{0,2}(卷标题|章标题)\*{0,2}\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)
MATERIAL_DIRECTORIES = (
    ("world", "世界观"),
    ("characters", "角色"),
    ("reference", "设定集 / 参考资料"),
)
MATERIAL_EXTENSIONS = {".md", ".txt"}


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


def _display_title(content: str, kind: str, fallback: str) -> str:
    expected_label = "卷标题" if kind == "volume_outline" else "章标题" if kind == "chapter_outline" else ""
    if expected_label:
        for label, value in METADATA_TITLE_PATTERN.findall(content):
            if label == expected_label:
                return re.sub(r"\*{1,2}|`", "", value).strip()
    heading = HEADING_PATTERN.search(content)
    if heading:
        return re.sub(r"^(?:卷纲|章纲)\s*[：:]\s*", "", heading.group(1)).strip()
    return fallback


def _document(project_dir: Path, path: Path, kind: str, volume: int, chapter: int = 0, section: int = 0) -> dict:
    content = path.read_text(encoding="utf-8")
    stat = path.stat()
    return {
        "id": f"{kind}:{volume}.{chapter}.{section}",
        "type": kind,
        "volume": volume,
        "chapter": chapter or None,
        "section": section or None,
        "title": _display_title(content, kind, _fallback_title(kind, volume, chapter, section)),
        "path": path.relative_to(project_dir).as_posix(),
        "char_count": len(re.sub(r"\s+", "", content)),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _material_document(project_dir: Path, path: Path, category_path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    stat = path.stat()
    heading = HEADING_PATTERN.search(content)
    relative_path = path.relative_to(category_path).as_posix()
    return {
        "id": f"material:{path.relative_to(project_dir).as_posix()}",
        "type": "material",
        "title": heading.group(1).strip() if heading else path.stem,
        "path": path.relative_to(project_dir).as_posix(),
        "relative_path": relative_path,
        "char_count": len(re.sub(r"\s+", "", content)),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _build_material_tree(project_dir: Path) -> tuple[list[dict], dict[str, tuple[dict, Path]]]:
    groups: dict[str, dict] = {}
    documents: dict[str, tuple[dict, Path]] = {}

    for relative_directory, title in MATERIAL_DIRECTORIES:
        category_path = project_dir / relative_directory
        if not category_path.is_dir():
            continue
        group = groups.setdefault(title, {"id": title, "title": title, "documents": []})
        for path in sorted(category_path.rglob("*"), key=lambda item: item.as_posix().lower()):
            if not path.is_file() or path.suffix.lower() not in MATERIAL_EXTENSIONS or path.name.startswith("."):
                continue
            document = _material_document(project_dir, path, category_path)
            group["documents"].append(document)
            documents[document["id"]] = (document, path)

    return list(groups.values()), documents


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

    for path in project_dir.glob("outlines/**/*.md"):
        relative_path = path.relative_to(project_dir).as_posix()
        volume_match = VOLUME_OUTLINE_PATTERN.match(relative_path)
        chapter_match = CHAPTER_OUTLINE_PATTERN.match(relative_path)
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

    for path in project_dir.glob("chapters/content_*.md"):
        match = SECTION_PATTERN.match(path.relative_to(project_dir).as_posix())
        if not match:
            continue
        volume, chapter, section = (int(value) for value in match.groups())
        document = _document(project_dir, path, "section", volume, chapter, section)
        ensure_chapter(volume, chapter)["sections"].append(document)
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


@router.get("/projects/{project_id}/materials")
async def get_material_tree(project_id: str, request: Request):
    """读取世界观、角色与参考资料，供小说工作区右侧资料栏展示。"""
    project_dir = _project_dir(project_id, request)
    groups, _ = _build_material_tree(project_dir)
    return {"project_id": project_id, "groups": groups}


@router.get("/projects/{project_id}/materials/nodes/{node_id:path}")
async def get_material_node(project_id: str, node_id: str, request: Request):
    """读取资料栏中一个已索引的创作资料文件。"""
    project_dir = _project_dir(project_id, request)
    _, documents = _build_material_tree(project_dir)
    item = documents.get(node_id)
    if item is None:
        raise HTTPException(status_code=404, detail="创作资料不存在")
    document, path = item
    return {"node": document, "content": path.read_text(encoding="utf-8")}
