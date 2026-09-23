"""Structured, read-only content checks."""

import re
from pathlib import Path

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult


class CheckContentTool(ToolProtocol):
    name = "CheckContent"
    description = "批量验收文件内容：检查必须包含、禁止残留、正则命中，或读取指定行片段。只读，不修改文件。"
    parameters = {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "description": "要执行的内容检查，按顺序返回结果",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                        "type": {
                            "type": "string",
                            "enum": ["contains", "not_contains", "regex", "excerpt"],
                        },
                        "pattern": {"type": "string", "description": "文本或正则；excerpt 不需要"},
                        "offset": {"type": "integer", "minimum": 1, "description": "excerpt 起始行"},
                        "limit": {"type": "integer", "minimum": 1, "description": "excerpt 行数"},
                    },
                    "required": ["path", "type"],
                },
            },
        },
        "required": ["checks"],
    }

    @staticmethod
    def _line_numbers(content: str, starts: list[int]) -> list[int]:
        return [content.count("\n", 0, start) + 1 for start in starts[:20]]

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        checks = params.get("checks")
        if not isinstance(checks, list) or not checks:
            return ToolResult(success=False, error="checks 不能为空")

        root = Path(context.working_dir).resolve()
        results = []
        for check in checks[:50]:
            path = str(check.get("path", ""))
            check_type = str(check.get("type", ""))
            file_path = (root / path).resolve()
            try:
                file_path.relative_to(root)
            except ValueError:
                results.append({"path": path, "type": check_type, "passed": False, "error": "路径超出项目目录"})
                continue
            if not file_path.is_file():
                results.append({"path": path, "type": check_type, "passed": False, "error": "文件不存在"})
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                results.append({"path": path, "type": check_type, "passed": False, "error": "无法以UTF-8编码读取文件"})
                continue

            if check_type == "excerpt":
                offset = max(1, int(check.get("offset", 1)))
                limit = max(1, int(check.get("limit", 20)))
                lines = content.splitlines(keepends=True)
                excerpt = "".join(lines[offset - 1:offset - 1 + limit])
                results.append({
                    "path": path, "type": check_type, "passed": offset <= len(lines),
                    "offset": offset, "limit": limit, "total_lines": len(lines), "content": excerpt,
                })
                continue

            pattern = str(check.get("pattern", ""))
            if not pattern:
                results.append({"path": path, "type": check_type, "passed": False, "error": "缺少 pattern"})
                continue
            if check_type in {"contains", "not_contains"}:
                starts = [match.start() for match in re.finditer(re.escape(pattern), content)]
                found = bool(starts)
                passed = found if check_type == "contains" else not found
            elif check_type == "regex":
                try:
                    starts = [match.start() for match in re.finditer(pattern, content, re.MULTILINE)]
                except re.error as exc:
                    results.append({"path": path, "type": check_type, "passed": False, "error": f"正则无效: {exc}"})
                    continue
                passed = bool(starts)
            else:
                results.append({"path": path, "type": check_type, "passed": False, "error": "不支持的检查类型"})
                continue
            results.append({
                "path": path, "type": check_type, "pattern": pattern, "passed": passed,
                "match_count": len(starts), "line_numbers": self._line_numbers(content, starts),
            })

        return ToolResult(success=True, data={
            "passed": bool(results) and all(item.get("passed", False) for item in results),
            "results": results,
        })

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        root = Path(context.working_dir).resolve()
        for check in params.get("checks", []):
            try:
                (root / str(check.get("path", ""))).resolve().relative_to(root)
            except ValueError:
                return PermissionResult.ASK
        return PermissionResult.ALLOW
