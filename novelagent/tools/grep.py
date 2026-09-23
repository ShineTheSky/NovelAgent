"""Grep工具 — ripgrep封装 + Python fallback"""

import re
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from fnmatch import fnmatch
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class GrepTool(ToolProtocol):
    name = "Grep"
    description = "在文件中搜索匹配正则表达式的内容。是所有检索操作的统一入口。用于查找人物信息、剧情线索、记忆内容等。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式搜索模式"},
            "path": {"type": "string", "description": "搜索目录，默认为项目根目录"},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "要搜索的多个文件或目录；设置后优先于path"},
            "glob": {"type": "string", "description": "文件名过滤，如 '*.md' 或 '.memory/*.md'"},
            "exclude_globs": {"type": "array", "items": {"type": "string"}, "description": "要排除的路径glob，如 ['.memory/**', '.insight/**']"},
            "regex_mode": {"type": "string", "enum": ["regex", "fixed"], "default": "regex", "description": "regex按正则匹配；fixed按原文匹配"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "content"},
            "context_lines": {"type": "integer", "description": "显示匹配行前后N行"},
            "max_results": {"type": "integer", "description": "最大返回结果数", "default": 50},
        },
        "required": ["pattern"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        root = Path(context.working_dir)
        requested = params.get("paths") or [params.get("path", "")]
        search_paths = [root / str(path) for path in requested]
        missing = [str(path) for path in search_paths if not path.exists()]
        if missing:
            return ToolResult(success=False, error=f"搜索路径不存在: {', '.join(missing)}")
        excluded = params.get("exclude_globs", [])

        def explicitly_excluded(path: Path) -> bool:
            try:
                relative = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                relative = str(path).replace("\\", "/")
            return any(fnmatch(relative, pattern) or fnmatch(path.name, pattern) for pattern in excluded)

        search_paths = [
            path for path in search_paths
            if not path.is_file() or not explicitly_excluded(path)
        ]
        if not search_paths:
            return ToolResult(success=True, data="无匹配结果")

        # Try ripgrep first, fall back to Python
        rg = shutil.which("rg")
        if rg:
            return self._execute_rg(rg, params, search_paths)

        for p in [r"C:\Program Files\ripgrep\rg.exe", "/usr/bin/rg", "/usr/local/bin/rg"]:
            if Path(p).exists():
                return self._execute_rg(p, params, search_paths)

        return self._execute_python(params, search_paths, root)

    def _execute_rg(self, rg_path: str, params: dict, search_paths: list[Path]) -> ToolResult:
        cmd = [rg_path, "--json"]
        if params.get("regex_mode", "regex") == "fixed":
            cmd.append("-F")
        if params.get("glob"):
            cmd.extend(["-g", params["glob"]])
        for excluded in params.get("exclude_globs", []):
            cmd.extend(["-g", f"!{excluded}"])
        if params.get("context_lines"):
            cmd.extend(["-C", str(params["context_lines"])])
        cmd.append(params["pattern"])
        cmd.extend(str(path) for path in search_paths)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode not in {0, 1}:
                return ToolResult(success=False, error=result.stderr.strip() or "Grep执行失败")
            output = self._parse_rg_output(result.stdout, params)
            return ToolResult(success=True, data=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Grep超时（10s）")

    def _parse_rg_output(self, stdout: str, params: dict) -> str:
        lines = stdout.strip().split("\n")
        if not lines or lines == [""]:
            return "无匹配结果"
        max_results = params.get("max_results", 50)
        matches = []
        for line in lines:
            try:
                data = json.loads(line)
                rtype = data.get("type", "")
                if rtype == "match":
                    mdata = data.get("data", {})
                    path = mdata.get("path", {}).get("text", "")
                    line_num = mdata.get("line_number", 0)
                    text = mdata.get("lines", {}).get("text", "").rstrip("\n")
                    matches.append((path, line_num, text))
            except json.JSONDecodeError:
                continue

        output_mode = params.get("output_mode", "content")
        if output_mode == "files_with_matches":
            results = list(dict.fromkeys(path for path, _, _ in matches))
        elif output_mode == "count":
            counts = Counter(path for path, _, _ in matches)
            results = [f"{path}: {count}" for path, count in counts.items()]
        else:
            results = [f"{path}:{line_num}: {text}" for path, line_num, text in matches]
        if len(results) > max_results:
            results = results[:max_results]
            results.append(f"[结果已截断，限制{max_results}条]")
        return "\n".join(results) if results else "无匹配结果"

    def _execute_python(self, params: dict, search_paths: list[Path], root: Path) -> ToolResult:
        """Pure Python fallback grep"""
        source_pattern = params["pattern"]
        try:
            pattern = re.compile(re.escape(source_pattern) if params.get("regex_mode", "regex") == "fixed" else source_pattern)
        except re.error as exc:
            return ToolResult(success=False, error=f"正则无效: {exc}")
        glob_filter = params.get("glob")
        exclude_globs = params.get("exclude_globs", [])
        max_results = params.get("max_results", 50)
        context_lines = params.get("context_lines", 0)
        output_mode = params.get("output_mode", "content")

        results = []
        matched_files = set()

        try:
            candidates = []
            seen = set()
            for search_path in search_paths:
                items = [search_path] if search_path.is_file() else search_path.rglob("*")
                for file_path in items:
                    key = str(file_path.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        candidates.append(file_path)
            for file_path in candidates:
                if not file_path.is_file():
                    continue
                try:
                    rel_path = str(file_path.relative_to(root)).replace("\\", "/")
                except ValueError:
                    rel_path = str(file_path).replace("\\", "/")
                if glob_filter and not (fnmatch(file_path.name, glob_filter) or fnmatch(rel_path, glob_filter)):
                    continue
                if any(fnmatch(rel_path, excluded) or fnmatch(file_path.name, excluded)
                       for excluded in exclude_globs):
                    continue

                try:
                    with open(file_path, encoding="utf-8") as f:
                        lines = f.readlines()
                except (UnicodeDecodeError, PermissionError):
                    continue

                file_matches = 0
                for i, line in enumerate(lines):
                    if pattern.search(line):
                        file_matches += 1
                        matched_files.add(rel_path)
                        if output_mode == "count":
                            continue
                        if output_mode == "content" and len(results) < max_results:
                            if context_lines > 0:
                                start = max(0, i - context_lines)
                                end = min(len(lines), i + context_lines + 1)
                                ctx_text = "".join(lines[start:end]).rstrip()
                                results.append(f"{rel_path}:{i+1}:\n{ctx_text}")
                            else:
                                results.append(f"{rel_path}:{i+1}: {line.rstrip()}")

                if output_mode == "count" and file_matches > 0:
                    results.append(f"{rel_path}: {file_matches}")

            if output_mode == "files_with_matches":
                results = list(matched_files)

            if len(results) > max_results:
                results = results[:max_results]
                results.append(f"[结果已截断，限制{max_results}条]")

            return ToolResult(success=True, data="\n".join(results) if results else "无匹配结果")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        root = Path(context.working_dir).resolve()
        for path in params.get("paths") or [params.get("path", "")]:
            try:
                (root / str(path)).resolve().relative_to(root)
            except ValueError:
                return PermissionResult.ASK
        return PermissionResult.ALLOW
