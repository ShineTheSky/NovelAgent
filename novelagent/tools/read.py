"""Read工具"""

from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class ReadTool(ToolProtocol):
    name = "Read"
    description = "读取文件内容。支持指定行范围、分页读取大文件。不指定offset/limit时默认读取前2000行。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对项目根目录）"},
            "offset": {"type": "integer", "description": "起始行号（默认1）"},
            "limit": {"type": "integer", "description": "读取行数（默认2000）"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        from pathlib import Path
        file_path = Path(context.working_dir) / params["path"]
        if not file_path.exists():
            return ToolResult(success=False, error=f"文件不存在: {params['path']}")
        if not file_path.is_file():
            return ToolResult(success=False, error=f"不是文件: {params['path']}")

        offset = params.get("offset", 1)
        limit = params.get("limit", 2000)

        try:
            with open(file_path, encoding="utf-8") as f:
                lines = f.readlines()
            total_lines = len(lines)
            if offset > total_lines:
                return ToolResult(success=True, data=f"[文件共{total_lines}行，offset={offset}超出范围]")
            selected = lines[offset - 1 : offset - 1 + limit]
            result = "".join(selected)
            if offset + limit <= total_lines:
                result += f"\n[已显示第{offset}-{offset + len(selected) - 1}行，共{total_lines}行]"
            return ToolResult(success=True, data=result)
        except UnicodeDecodeError:
            return ToolResult(success=False, error="无法以UTF-8编码读取文件")

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        from pathlib import Path
        resolved = (Path(context.working_dir) / params["path"]).resolve()
        if str(resolved).startswith(str(Path(context.working_dir).resolve())):
            return PermissionResult.ALLOW
        return PermissionResult.ASK
