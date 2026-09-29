"""Explicitly attach a no-change decision to the evaluated document version."""

from pathlib import Path

from novelagent.history import DocumentHistoryStore
from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult
from novelagent.versioning import revision_manager


class RecordNoChangeTool(ToolProtocol):
    name = "RecordNoChange"
    description = "仅在明确判断某份创作文件无需修改时，记录原始意见和无需修改的依据；不创建正文版本。失败或尚待处理不能使用。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "被评价的项目内 Markdown 文件路径"},
            "expected_revision_id": {"type": "string", "description": "被评价文件的当前修订 ID"},
            "opinion": {"type": "string", "description": "用户或审阅 Agent 的意见原文"},
            "rationale": {"type": "string", "description": "明确无需修改的决定与依据原文"},
        },
        "required": ["path", "expected_revision_id", "opinion", "rationale"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        project_dir = Path(context.working_dir).resolve()
        path = (project_dir / str(params["path"])).resolve()
        if not path.is_relative_to(project_dir) or not revision_manager.is_managed(path, str(project_dir)):
            return ToolResult(success=False, error="仅可记录项目内创作 Markdown 文件")
        if not path.is_file():
            return ToolResult(success=False, error="被评价文件不存在")
        if not str(params["opinion"]).strip() or not str(params["rationale"]).strip():
            return ToolResult(success=False, error="意见和无需修改的依据均不能为空")
        try:
            history = DocumentHistoryStore(project_dir)
            revision_id = history.ensure_baseline(path)
            if revision_id != params["expected_revision_id"]:
                return ToolResult(success=False, error=f"revision_conflict: 当前版本 {revision_id}")
            request_id = str(context.history_evidence.get("history_request_id") or "")
            history.record_no_change(
                revision_id, str(params["opinion"]), str(params["rationale"]),
                request_id=request_id, path=str(params["path"]),
            )
            return ToolResult(success=True, data=f"无需修改的决定已记录于 {params['path']}@{revision_id}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        project_dir = Path(context.working_dir).resolve()
        path = (project_dir / str(params.get("path") or "")).resolve()
        return PermissionResult.ALLOW if path.is_relative_to(project_dir) else PermissionResult.BLOCK
