"""Aggregate all Bash cases into review-only monitored-tool candidates."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


class BashCaseAnalyzer:
    """Analyze repeated command families, including successful executions."""

    def __init__(self, llm_client, workspace_dir: str, config: dict | None = None):
        settings = config or {}
        self.llm = llm_client
        self.directory = Path(workspace_dir).resolve().parent / "data" / "bash_cases"
        self.enabled = bool(settings.get("enabled", True))
        self.batch_size = max(2, int(settings.get("batch_size", 3)))
        self.max_cases = max(self.batch_size, int(settings.get("max_cases", 20)))
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def classify(command: str) -> str:
        """Use the first executable token as a stable, cheap command family."""
        clean = command.strip()
        clean = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)+", "", clean)
        token = re.split(r"\s+|[|;&]", clean, maxsplit=1)[0].lower()
        aliases = {"rg": "grep", "egrep": "grep", "fgrep": "grep", "dir": "ls", "type": "cat"}
        return aliases.get(token, token or "unknown")

    def schedule(self, payload: dict) -> None:
        if self.enabled and self.llm:
            asyncio.create_task(self.analyze_when_ready(str(payload.get("command_family", "unknown"))))

    async def analyze_when_ready(self, family: str) -> None:
        async with self._locks[family]:
            cases = await asyncio.to_thread(self._load_cases, family)
            if len(cases) < self.batch_size or len(cases) % self.batch_size:
                return
            await self._analyze(family, cases[-self.max_cases:])

    def _load_cases(self, family: str) -> list[dict]:
        cases = []
        for path in self.directory.rglob("*.json"):
            if "analysis" in path.parts:
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("command_family") == family:
                    cases.append(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(cases, key=lambda item: str(item.get("created_at", "")))

    async def _analyze(self, family: str, cases: list[dict]) -> None:
        samples = [{
            "case_id": item.get("id", ""), "command": item.get("command", ""),
            "permission": item.get("permission", ""), "outcome": item.get("outcome", ""),
            "result_preview": str(item.get("result_preview", ""))[:500],
            "error": str(item.get("error", ""))[:500],
        } for item in cases]
        prompt = f'''你是工具演化分析器。下面是 Bash 命令族 `{family}` 的完整行为样本，包含成功、失败、拦截和用户拒绝；不要把失败样本当成全部事实。
样本：{json.dumps(samples, ensure_ascii=False)}
只输出 JSON：{{"summary":"","observed_pattern":"","risks":[{{"risk":"","evidence_case_ids":[""]}}],"tool_candidates":[{{"name":"","purpose":"","parameters":"","permission":"allow|ask|block","evidence_case_ids":[""]}}],"needs_more_evidence":false}}。
只提出待人工审核的候选工具，不要声称已创建或已替换 Bash；如果样本不足、用途差异大或风险不清楚，needs_more_evidence=true。'''
        text = ""
        try:
            async for chunk in self.llm.chat(
                position="bash_case_analysis", messages=[{"role": "user", "content": prompt}],
                tools=None, stream=False, tag=":bash-case-analysis", max_tokens=2048,
            ):
                if chunk.type == "text_delta":
                    text += chunk.content
            data = self._json(text)
            if data:
                await asyncio.to_thread(self._write_analysis, family, cases, data)
        except Exception as exc:
            print(f"[bash_case] analysis failed for {family}: {exc}", flush=True)

    @staticmethod
    def _json(text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            value = json.loads(clean[clean.find("{"):clean.rfind("}") + 1])
            return value if isinstance(value, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _write_analysis(self, family: str, cases: list[dict], data: dict) -> None:
        directory = self.directory / "analysis"
        directory.mkdir(parents=True, exist_ok=True)
        outcomes = Counter(str(item.get("outcome", "unknown")) for item in cases)
        case_ids = [str(item.get("id", "")) for item in cases if item.get("id")]
        header = {
            "type": "bash_case_analysis", "command_family": family,
            "updated": datetime.now(timezone.utc).isoformat(), "case_count": len(case_ids),
            "source_case_ids": case_ids, "outcomes": dict(outcomes),
            "status": "review_required", "needs_more_evidence": bool(data.get("needs_more_evidence", False)),
        }
        lines = ["---", yaml.safe_dump(header, allow_unicode=True, sort_keys=False).strip(), "---", "", f"# Bash 命令族：{family}", "", "## 观察总结", str(data.get("summary", "")), "", "## 使用模式", str(data.get("observed_pattern", "")), "", "## 风险"]
        for item in data.get("risks", []):
            if isinstance(item, dict):
                lines.append(f"- {item.get('risk', '')}（证据：{', '.join(item.get('evidence_case_ids', []))}）")
        lines.extend(["", "## 待审核受监控工具候选"])
        for item in data.get("tool_candidates", []):
            if isinstance(item, dict):
                lines.append(f"- `{item.get('name', '')}`：{item.get('purpose', '')}；参数：{item.get('parameters', '')}；权限：{item.get('permission', '')}（证据：{', '.join(item.get('evidence_case_ids', []))}）")
        if not data.get("tool_candidates"):
            lines.append("- 暂无候选，等待更多样本。")
        (directory / f"{family}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
