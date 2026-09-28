"""Read a specific original document revision for memory analysis."""

from novelagent.history import DocumentHistoryStore
from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult


class GetHistoryRevisionTool(ToolProtocol):
    name = "GetHistoryRevision"
    description = "按 History 修订 ID 读取该版完整正文、父版本、上游依赖和原始需求/执行证据。仅在语义证据不足时调用。"
    parameters = {
        "type": "object",
        "properties": {"revision_id": {"type": "string", "description": "需回读的 History 修订 ID"}},
        "required": ["revision_id"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        revision = DocumentHistoryStore(context.working_dir).get_revision(str(params["revision_id"]))
        if revision is None:
            return ToolResult(success=False, error="History 修订不存在")
        return ToolResult(success=True, data=revision)

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW if context.actor == "trace_analyzer" else PermissionResult.BLOCK
