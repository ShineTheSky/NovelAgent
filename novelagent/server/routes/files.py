"""文件浏览API路由"""

from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, Query

from novelagent.security.path_validator import PathValidator

router = APIRouter(prefix="/api")

TEXT_EXTENSIONS = {".md", ".yaml", ".yml", ".txt", ".json"}
MAX_CONTENT_SIZE = 50 * 1024  # 50KB


def _build_tree(dir_path: Path, root_path: Path) -> dict | None:
    """递归构建目录树，排除 .开头的文件和目录。返回 None 表示空目录。"""
    name = dir_path.name or dir_path.resolve().name
    node = {"name": name, "path": str(dir_path.relative_to(root_path)).replace("\\", "/"), "type": "directory", "children": []}

    entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            child = _build_tree(entry, root_path)
            if child is not None:
                node["children"].append(child)
        elif entry.suffix.lower() in TEXT_EXTENSIONS:
            rel_path = str(entry.relative_to(root_path)).replace("\\", "/")
            node["children"].append({
                "name": entry.name,
                "path": rel_path,
                "type": "file",
                "size": entry.stat().st_size,
            })

    if not node["children"]:
        return None  # 空目录不展示
    return node


@router.get("/projects/{project_id}/files")
async def list_files(project_id: str, request: Request):
    """获取项目文件目录树"""
    working_dir = Path(request.app.state.config.get("working_dir", "./workspace"))
    project_dir = working_dir / project_id

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="项目目录不存在")

    tree = _build_tree(project_dir, project_dir)
    root = tree if tree else {"name": project_id, "path": "", "type": "directory", "children": []}

    return {"project_id": project_id, "tree": root}


@router.get("/projects/{project_id}/files/content")
async def get_file_content(project_id: str, request: Request, path: str = Query(...)):
    """获取单个文件内容"""
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数")

    ext = Path(path).suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    working_dir = Path(request.app.state.config.get("working_dir", "./workspace"))
    project_dir = working_dir / project_id
    validator = PathValidator(str(project_dir))

    if not validator.validate(path):
        raise HTTPException(status_code=403, detail="路径越界")

    file_path = project_dir / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="无法读取文件编码")

    size = len(content)
    truncated = size > MAX_CONTENT_SIZE
    if truncated:
        content = content[:MAX_CONTENT_SIZE] + "\n\n... (内容已截断)"

    return {"path": path, "content": content, "size": size}
