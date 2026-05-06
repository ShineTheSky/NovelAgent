"""Grep工具 — ripgrep封装 + Python fallback"""

import re
import json
import shutil
import subprocess
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
            "glob": {"type": "string", "description": "文件名过滤，如 '*.md' 或 '.memory/*.md'"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "content"},
            "context_lines": {"type": "integer", "description": "显示匹配行前后N行"},
            "max_results": {"type": "integer", "description": "最大返回结果数", "default": 50},
        },
        "required": ["pattern"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        pattern = params["pattern"]
        search_path = Path(context.working_dir) / params.get("path", "")
        if not search_path.exists():
            search_path = Path(context.working_dir)

        # Try ripgrep first, fall back to Python
        rg = shutil.which("rg")
        if rg:
            return self._execute_rg(rg, params, search_path)

        for p in [r"C:\Program Files\ripgrep\rg.exe", "/usr/bin/rg", "/usr/local/bin/rg"]:
            if Path(p).exists():
                return self._execute_rg(p, params, search_path)

        return self._execute_python(params, search_path)

    def _execute_rg(self, rg_path: str, params: dict, search_path: Path) -> ToolResult:
        cmd = [rg_path, "--json"]
        if params.get("glob"):
            cmd.extend(["-g", params["glob"]])
        if params.get("context_lines"):
            cmd.extend(["-C", str(params["context_lines"])])
        if params.get("output_mode") == "files_with_matches":
            cmd.append("-l")
        elif params.get("output_mode") == "count":
            cmd.append("-c")
        cmd.extend([params["pattern"], str(search_path)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            output = self._parse_rg_output(result.stdout, params)
            return ToolResult(success=True, data=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Grep超时（10s）")

    def _parse_rg_output(self, stdout: str, params: dict) -> str:
        lines = stdout.strip().split("\n")
        if not lines or lines == [""]:
            return "无匹配结果"
        max_results = params.get("max_results", 50)
        results = []
        for line in lines[:max_results * 5]:
            try:
                data = json.loads(line)
                rtype = data.get("type", "")
                if rtype == "match":
                    mdata = data.get("data", {})
                    path = mdata.get("path", {}).get("text", "")
                    line_num = mdata.get("line_number", 0)
                    text = mdata.get("lines", {}).get("text", "").rstrip("\n")
                    results.append(f"{path}:{line_num}: {text}")
                elif rtype == "summary":
                    elapsed = data.get("data", {}).get("elapsed_total", {})
                    if elapsed:
                        results.append(f"[耗时: {elapsed.get('secs', 0)}s]")
            except json.JSONDecodeError:
                results.append(line)

        if len(results) > max_results:
            results = results[:max_results]
            results.append(f"[结果已截断，限制{max_results}条]")
        return "\n".join(results) if results else "无匹配结果"

    def _execute_python(self, params: dict, search_path: Path) -> ToolResult:
        """Pure Python fallback grep"""
        pattern = re.compile(params["pattern"])
        glob_filter = params.get("glob")
        max_results = params.get("max_results", 50)
        context_lines = params.get("context_lines", 0)
        output_mode = params.get("output_mode", "content")

        results = []
        matched_files = set()

        try:
            for file_path in search_path.rglob("*"):
                if not file_path.is_file():
                    continue
                if glob_filter and not fnmatch(file_path.name, glob_filter):
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
                        matched_files.add(str(file_path))
                        if output_mode == "count":
                            continue
                        if output_mode == "content" and len(results) < max_results:
                            rel_path = str(file_path.relative_to(search_path)).replace("\\", "/")
                            if context_lines > 0:
                                start = max(0, i - context_lines)
                                end = min(len(lines), i + context_lines + 1)
                                ctx_text = "".join(lines[start:end]).rstrip()
                                results.append(f"{rel_path}:{i+1}:\n{ctx_text}")
                            else:
                                results.append(f"{rel_path}:{i+1}: {line.rstrip()}")

                if output_mode == "count" and file_matches > 0:
                    rel_path = str(file_path.relative_to(search_path)).replace("\\", "/")
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
        return PermissionResult.ALLOW
