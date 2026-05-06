"""项目API路由"""

from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class CreateProjectRequest(BaseModel):
    name: str
    genre: str = ""


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
