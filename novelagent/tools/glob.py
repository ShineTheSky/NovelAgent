"""Glob工具"""

from datetime import datetime
from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class GlobTool(ToolProtocol):
    name = "Glob"
    description = "按文件名模式查找文件。用于发现项目结构、浏览记忆目录。支持递归**模式。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob模式，如 'chapters/**/*.md', '.memory/**/*.md'"},
            "path": {"type": "string", "description": "搜索目录，默认为项目根目录"},
            "include_metadata": {"type": "boolean", "description": "是否返回类型、文件大小和修改时间", "default": False},
        },
        "required": ["pattern"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        base = Path(context.working_dir) / params.get("path", "")
        if not base.exists():
            base = Path(context.working_dir)

        matches = sorted(base.glob(params["pattern"]), key=lambda item: str(item).lower())
        # 转为相对路径
        rel_paths = []
        for m in matches:
            try:
                rel = m.relative_to(context.working_dir)
                display = str(rel).replace("\\", "/")
            except ValueError:
                display = str(m).replace("\\", "/")
            if params.get("include_metadata"):
                try:
                    stat = m.stat()
                    kind = "directory" if m.is_dir() else "file"
                    size = None if m.is_dir() else stat.st_size
                    modified_at = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
                    display = f"{display}\ttype={kind}\tsize={size if size is not None else '-'}\tmodified_at={modified_at}"
                except OSError:
                    display = f"{display}\tmetadata=unavailable"
            rel_paths.append(display)

        if not rel_paths:
            return ToolResult(success=True, data="(无匹配文件)")
        return ToolResult(success=True, data="\n".join(rel_paths[:100]))  # 上限100条，每行一个路径

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW
