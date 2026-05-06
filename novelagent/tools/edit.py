"""Edit工具"""

from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult
from novelagent.tools.write import WriteTool


class EditTool(ToolProtocol):
    name = "Edit"
    description = "精确替换文件中的指定字符串。old_string必须唯一匹配。支持replace_all替换所有匹配。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要替换的原文本（必须唯一匹配，除非replace_all=true）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认false）", "default": False},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        file_path = Path(context.working_dir) / params["path"]
        if not file_path.exists():
            return ToolResult(success=False, error=f"文件不存在: {params['path']}")

        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(success=False, error=f"读取文件失败: {e}")

        old = params["old_string"]
        new = params["new_string"]
        replace_all = params.get("replace_all", False)

        count = content.count(old)
        if count == 0:
            return ToolResult(success=False, error="未找到匹配文本")
        if count > 1 and not replace_all:
            return ToolResult(success=False, error=f"old_string匹配不唯一（共{count}处），请设置replace_all=true或提供更精确的上下文")

        content = content.replace(old, new) if replace_all else content.replace(old, new, 1)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            replaced = "全部" if replace_all else "1处"
            return ToolResult(success=True, data=f"文件: {params['path']}\n替换: 共{count}处匹配, 已替换{replaced}\n===EDIT_DIFF===\n旧: {old}\n新: {new}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    # 复用 Write 的 checkPermissions 逻辑
    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return WriteTool.checkPermissions(self, params, context)
