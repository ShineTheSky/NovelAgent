"""Read-only text statistics for project files."""

import re
from pathlib import Path

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult
from novelagent.versioning import revision_manager


class TextStatsTool(ToolProtocol):
    name = "TextStats"
    description = "统计文本文件的字符数、汉字数、非空白字符数和行数；可自动排除 Markdown frontmatter。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对项目根目录）"},
            "strip_frontmatter": {
                "type": "boolean",
                "description": "是否排除 Markdown YAML frontmatter，默认 true",
                "default": True,
            },
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        file_path = Path(context.working_dir) / params["path"]
        if not file_path.is_file():
            return ToolResult(success=False, error=f"文件不存在: {params['path']}")
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(success=False, error="无法以UTF-8编码读取文件")

        revision_id = ""
        text = content
        if params.get("strip_frontmatter", True) and file_path.suffix.lower() == ".md":
            info = revision_manager.parse(content)
            if not info.legacy:
                text = info.body
                revision_id = info.revision_id
            else:
                text = re.sub(r"\A---\s*\n.*?\n---\s*\n?", "", content, count=1, flags=re.DOTALL)

        han_characters = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
        latin_words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))

        return ToolResult(success=True, data={
            "path": params["path"],
            "revision_id": revision_id,
            "characters": len(text),
            "characters_no_whitespace": len(re.sub(r"\s", "", text)),
            "han_characters": han_characters,
            "word_count": han_characters + latin_words,
            "lines": len(text.splitlines()),
        })

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        resolved = (Path(context.working_dir) / params["path"]).resolve()
        try:
            resolved.relative_to(Path(context.working_dir).resolve())
            return PermissionResult.ALLOW
        except ValueError:
            return PermissionResult.ASK
