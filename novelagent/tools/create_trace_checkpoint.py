"""主 Agent 主动关闭当前 Trace 聚合窗口。"""

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult


class CreateTraceCheckpointTool(ToolProtocol):
    name = "CreateTraceCheckpoint"
    description = "当用户提出关键纠错、长期偏好、明确确认或重要项目设定时，提前记录当前聚合 Trace 并安排后台分析。"
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "触发原因的简短说明"},
        },
        "required": ["reason"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, data={"queued": True, "reason": params["reason"][:200]})

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW
