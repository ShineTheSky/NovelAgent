"""Glob工具"""

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
        },
        "required": ["pattern"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        base = Path(context.working_dir) / params.get("path", "")
        if not base.exists():
            base = Path(context.working_dir)

        matches = list(base.glob(params["pattern"]))
        # 转为相对路径
        rel_paths = []
        for m in matches:
            try:
                rel = m.relative_to(context.working_dir)
                rel_paths.append(str(rel).replace("\\", "/"))
            except ValueError:
                rel_paths.append(str(m).replace("\\", "/"))

        if not rel_paths:
            return ToolResult(success=True, data="(无匹配文件)")
        return ToolResult(success=True, data="\n".join(rel_paths[:100]))  # 上限100条，每行一个路径

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW
