"""主 Agent 主动关闭当前 Trace 聚合窗口。"""

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult


class CreateTraceCheckpointTool(ToolProtocol):
    name = "CreateTraceCheckpoint"
    description = "仅在诊断需要时提前关闭当前 Trace 摘要窗口；普通记忆由 History 在请求结束后提炼。"
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
