"""Write工具"""

import hashlib
import re
from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult
from novelagent.versioning import RevisionConflict, revision_manager

PROTECTED_DIRS = [".git", ".claude/settings", ".env", ".memory/memory.md"]
INTERNAL_EDITABLES = [".claude/plans/", ".claude/scratchpad.md"]


def _changed_excerpt(before: str, after: str, limit: int = 1200) -> tuple[str, str]:
    """Return bounded text around the first changed position for Trace anchoring."""
    shared = 0
    max_shared = min(len(before), len(after))
    while shared < max_shared and before[shared] == after[shared]:
        shared += 1
    start = max(0, shared - 180)
    before_excerpt = before[start:start + limit].strip()
    after_excerpt = after[start:start + limit].strip()
    return before_excerpt, after_excerpt


def layout_error(path: str) -> str | None:
    """Reject malformed creative-file paths before an agent can recreate legacy layouts."""
    normalized = path.replace("\\", "/").lstrip("./")
    canonical = (
        re.fullmatch(r"outlines/outline_\d+\.0\.0\.md", normalized)
        or re.fullmatch(r"outlines/outline_\d+\.\d+\.0\.md", normalized)
        or re.fullmatch(r"chapters/content_\d+\.\d+\.\d+\.md", normalized)
        or re.fullmatch(r"(?:world|characters|reference)/.+\.md", normalized)
    )
    if canonical:
        return None
    if normalized.startswith(("outlines/", "manuscript/", "materials/", "chapters/", "world/", "characters/", "reference/", "project/characters/")) or normalized == "outline.md" or normalized.startswith("outline_"):
        return (
            "项目创作文件必须使用规范路径：卷纲为 outlines/outline_X.0.0.md，"
            "章纲为 outlines/outline_X.X.0.md，正文为 chapters/content_X.X.n.md，"
            "资料为 world/、characters/ 或 reference/。"
        )
    return None


class WriteTool(ToolProtocol):
    name = "Write"
    description = "创建新文件或覆盖已有文件。自动创建父目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对项目根目录）"},
            "content": {"type": "string", "description": "要写入的完整内容"},
            "expected_revision_id": {"type": "string", "description": "受版本保护的 Markdown 文件必须提供：Read 返回的当前修订；新文件使用 new"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        error = layout_error(params["path"])
        if error:
            return ToolResult(success=False, error=error)
        file_path = Path(context.working_dir) / params["path"]
        try:
            if revision_manager.is_managed(file_path, context.working_dir):
                existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
                current = revision_manager.parse(existing) if existing else None
                info = await revision_manager.commit(
                    file_path, params["content"], params.get("expected_revision_id"),
                    actor=context.actor, operation_id=context.operation_id,
                )
                before_excerpt, after_excerpt = _changed_excerpt(current.body if current else "", info.body)
                context.revision_events.append({
                    "path": params["path"], "revision_id": info.revision_id,
                    "parent_revision_id": info.metadata.get("parent_revision_id"),
                    "source_revision_id": current.revision_id if current else "new",
                    "anchor_excerpt": before_excerpt,
                    "anchor_sha256": hashlib.sha256(before_excerpt.encode("utf-8")).hexdigest() if before_excerpt else "",
                    "replacement_excerpt": after_excerpt,
                    "updated_by": context.actor,
                    "operation_id": context.operation_id,
                })
                return ToolResult(success=True, data=(
                    f"文件已写入: {params['path']}\n当前修订: {info.revision_id}\n"
                    f"父修订: {info.metadata.get('parent_revision_id') or '无（新文件）'}"
                ))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(params["content"])
            content = params["content"]
            return ToolResult(success=True, data=f"文件已写入: {params['path']}\n\n--- 写入内容 ---\n{content}")
        except RevisionConflict as conflict:
            return ToolResult(success=False, error=(
                f"revision_conflict: 期望版本 {params.get('expected_revision_id') or '(缺失)'}，"
                f"当前版本 {conflict.info.revision_id}。请读取并基于最新内容重试。\n\n"
                f"--- 最新内容 ---\n{conflict.info.body}"
            ))
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
