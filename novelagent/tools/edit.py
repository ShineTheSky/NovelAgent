"""Edit工具"""

from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult
from novelagent.tools.write import WriteTool, layout_error
from novelagent.versioning import RevisionConflict, revision_manager


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
            "expected_revision_id": {"type": "string", "description": "受版本保护的 Markdown 文件必须提供 Read 返回的当前修订"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        error = layout_error(params["path"])
        if error:
            return ToolResult(success=False, error=error)
        file_path = Path(context.working_dir) / params["path"]
        if not file_path.exists():
            return ToolResult(success=False, error=f"文件不存在: {params['path']}")

        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(success=False, error=f"读取文件失败: {e}")

        managed = revision_manager.is_managed(file_path, context.working_dir)
        current = revision_manager.parse(content) if managed else None
        if managed and params.get("expected_revision_id", "") != current.revision_id:
            return ToolResult(success=False, error=(
                f"revision_conflict: 期望版本 {params.get('expected_revision_id') or '(缺失)'}，当前版本 {current.revision_id}。"
                f"请读取并基于最新内容重试。\n\n--- 最新内容 ---\n{current.body}"
            ))

        old = params["old_string"]
        new = params["new_string"]
        replace_all = params.get("replace_all", False)

        editable = current.body if managed else content
        count = editable.count(old)
        if count == 0:
            return ToolResult(success=False, error="未找到匹配文本")
        if count > 1 and not replace_all:
            return ToolResult(success=False, error=f"old_string匹配不唯一（共{count}处），请设置replace_all=true或提供更精确的上下文")

        updated = editable.replace(old, new) if replace_all else editable.replace(old, new, 1)

        try:
            if managed:
                info = await revision_manager.commit(
                    file_path, updated, params.get("expected_revision_id"),
                    actor=context.actor, operation_id=context.operation_id,
                )
                context.revision_events.append({
                    "path": params["path"], "revision_id": info.revision_id,
                    "parent_revision_id": info.metadata.get("parent_revision_id"),
                    "updated_by": context.actor,
                    "operation_id": context.operation_id,
                })
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(updated)
            replaced = "全部" if replace_all else "1处"
            revision_line = f"\n当前修订: {info.revision_id}" if managed else ""
            return ToolResult(success=True, data=f"文件: {params['path']}\n替换: 共{count}处匹配, 已替换{replaced}{revision_line}\n===EDIT_DIFF===\n旧: {old}\n新: {new}")
        except RevisionConflict as conflict:
            return ToolResult(success=False, error=(
                f"revision_conflict: 当前版本 {conflict.info.revision_id}。请读取并基于最新内容重试。\n\n"
                f"--- 最新内容 ---\n{conflict.info.body}"
            ))
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    # 复用 Write 的 checkPermissions 逻辑
    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return WriteTool.checkPermissions(self, params, context)
