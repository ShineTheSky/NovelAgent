"""Write工具"""

from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult

PROTECTED_DIRS = [".git", ".claude/settings", ".env", ".memory/memory.md"]
INTERNAL_EDITABLES = [".claude/plans/", ".claude/scratchpad.md"]


class WriteTool(ToolProtocol):
    name = "Write"
    description = "创建新文件或覆盖已有文件。自动创建父目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对项目根目录）"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        file_path = Path(context.working_dir) / params["path"]
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(params["content"])
            content = params["content"]
            return ToolResult(success=True, data=f"文件已写入: {params['path']}\n\n--- 写入内容 ---\n{content}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        resolved = str((Path(context.working_dir) / params["path"]).resolve())
        working = str(Path(context.working_dir).resolve())

        # 0. 写入 .memory/ 目录 → ALLOW（记忆系统免确认）
        norm = params["path"].replace("\\", "/")
        if norm.startswith(".memory/"):
            return PermissionResult.ALLOW

        # 1. 路径在工作目录外 → ASK
        if not resolved.startswith(working):
            return PermissionResult.ASK

        # 2. 保护目录 → ASK (不可绕过)
        for pd in PROTECTED_DIRS:
            if pd in resolved.replace("\\", "/"):
                return PermissionResult.ASK

        # 3. 匹配 allow_rules → ALLOW
        for rule in context.allow_rules:
            if Path(params["path"]).match(rule.get("pattern", "")):
                return PermissionResult.ALLOW

        # 4. acceptEdits 模式 → ALLOW
        if context.accept_edits_mode:
            return PermissionResult.ALLOW

        # 5. 内部可编辑文件 → ALLOW
        for ie in INTERNAL_EDITABLES:
            if norm.startswith(ie) or norm == ie:
                return PermissionResult.ALLOW

        # 6. 会话内已批准 → ALLOW
        if context.is_previously_allowed(f"write:{params['path']}"):
            return PermissionResult.ALLOW

        # 7. 其余 → ASK
        return PermissionResult.ASK
