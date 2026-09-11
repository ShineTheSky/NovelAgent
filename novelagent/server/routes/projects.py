"""项目API路由"""

from pathlib import Path
from uuid import UUID

import yaml
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class CreateProjectRequest(BaseModel):
    name: str
    genre: str = ""


class ImportProjectRequest(BaseModel):
    project_id: str


def _existing_project_metadata(project_dir: Path) -> dict:
    project_file = project_dir / "project.yaml"
    if not project_file.exists():
        raise HTTPException(status_code=404, detail="项目目录缺少 project.yaml")
    try:
        metadata = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise HTTPException(status_code=400, detail=f"project.yaml 无法解析: {error}")
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="project.yaml 格式无效")
    return metadata


def _import_candidate(project_dir: Path) -> dict:
    metadata = _existing_project_metadata(project_dir)
    try:
        UUID(project_dir.name)
    except ValueError:
        raise HTTPException(status_code=400, detail="项目目录名必须是 UUID")
    stored_id = str(metadata.get("project_id", "")).strip()
    if stored_id and stored_id != project_dir.name:
        raise HTTPException(status_code=400, detail="project.yaml 中的 project_id 与目录名不一致")
    try:
        word_count = max(0, int(metadata.get("word_count", 0)))
    except (TypeError, ValueError):
        word_count = 0
    return {
        "project_id": project_dir.name,
        "name": str(metadata.get("name") or project_dir.name),
        "genre": str(metadata.get("genre") or ""),
        "word_count": word_count,
    }


@router.post("/projects")
async def create_project(body: CreateProjectRequest, request: Request):
    """创建新项目 + 脚手架"""
    from novelagent.storage import models
    from novelagent.memory.memory_manager import MemoryManager

    project = await models.create_project(body.name, body.genre)

    # Create project directory structure
    working_dir = Path(request.app.state.config.get("working_dir", "./workspace"))
    project_dir = working_dir / project.project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "chapters").mkdir(exist_ok=True)

    # Create project.yaml
    import yaml
    project_yaml = project_dir / "project.yaml"
    project_yaml.write_text(yaml.dump({
        "name": body.name,
        "genre": body.genre,
        "project_id": project.project_id,
        "word_count": 0,
        "created": project.created_at,
    }, allow_unicode=True), encoding="utf-8")

    # Initialize memory
    memory = MemoryManager(str(project_dir))
    memory.init_project_memory()

    return {
        "project_id": project.project_id,
        "name": project.name,
        "genre": project.genre,
        "created_at": project.created_at,
    }


@router.get("/projects")
async def list_projects(request: Request):
    """获取项目列表"""
    from novelagent.storage import models
    projects = await models.list_projects()
    return [{"project_id": p.project_id, "name": p.name, "genre": p.genre,
             "word_count": p.word_count, "created_at": p.created_at} for p in projects]


@router.get("/projects/importable")
async def list_importable_projects(request: Request):
    """列出工作区中存在、但尚未登记到 SQLite 的项目目录。"""
    from novelagent.storage import models

    registered = {project.project_id for project in await models.list_projects()}
    working_dir = Path(request.app.state.config.get("working_dir", "./workspace"))
    if not working_dir.exists():
        return []

    candidates = []
    for project_dir in working_dir.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith(".") or project_dir.name in registered:
            continue
        try:
            candidates.append(_import_candidate(project_dir))
        except HTTPException:
            continue
    return sorted(candidates, key=lambda item: item["name"].lower())


@router.post("/projects/import")
async def import_existing_project(body: ImportProjectRequest, request: Request):
    """将现有工作区目录登记为项目，不改写其中任何文件。"""
    try:
        project_id = str(UUID(body.project_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的项目 ID")

    working_dir = Path(request.app.state.config.get("working_dir", "./workspace"))
    candidate = _import_candidate(working_dir / project_id)
    from novelagent.storage.database import get_connection

    conn = await get_connection()
    try:
        existing = await (await conn.execute("SELECT project_id FROM projects WHERE project_id = ?", (project_id,))).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="项目已导入")
        await conn.execute(
            "INSERT INTO projects (project_id, name, genre, word_count) VALUES (?, ?, ?, ?)",
            (candidate["project_id"], candidate["name"], candidate["genre"], candidate["word_count"]),
        )
        await conn.commit()
    finally:
        await conn.close()

    return candidate


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    """删除项目"""
    from novelagent.storage.database import get_connection
    conn = await get_connection()
    await conn.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
    await conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    await conn.commit()
    await conn.close()
    return {"status": "deleted"}
