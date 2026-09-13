"""Batch analysis for global Agent Bad Cases; never modifies prompts or tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


class BadCaseAnalyzer:
    """Classify raw failures, then periodically derive review-only diagnoses."""

    def __init__(self, llm_client, workspace_dir: str, config: dict | None = None):
        settings = config or {}
        self.llm = llm_client
        self.directory = Path(workspace_dir).resolve().parent / "data" / "agent_bad_cases"
        self.enabled = bool(settings.get("enabled", True))
        self.batch_size = max(2, int(settings.get("batch_size", 3)))
        self.max_cases = max(self.batch_size, int(settings.get("max_cases", 12)))
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def classify(metadata: dict) -> str:
        """Cheap deterministic routing; the LLM only analyzes a repeated category."""
        failure_kind = str(metadata.get("failure_kind", "")).lower()
        tool = str(metadata.get("tool", "")).lower()
        error = str(metadata.get("error", "")).lower()
        if any(marker in error for marker in ("timeout", "超时", "timed out")):
            return "llm_timeout" if "agent" in failure_kind else "tool_timeout"
        if any(marker in error for marker in ("http 4", "http 5", "api", "deserialize", "认证", "unauthorized", "invalid api")):
            return "llm_api"
        if any(marker in error for marker in ("安全策略", "permission", "用户拒绝")):
            return "tool_permission"
        if "workflow" in failure_kind or "workflow" in tool:
            return "workflow"
        if "subagent" in failure_kind or tool == "subagent":
            return "subagent"
        if tool == "bash":
            return "bash_execution"
        if tool:
            return "tool_execution"
        return "agent_execution"

    def schedule(self, trace_id: str, metadata: dict) -> None:
        if self.enabled and self.llm:
            asyncio.create_task(self.analyze_when_ready(trace_id, metadata))

    async def analyze_when_ready(self, trace_id: str, metadata: dict) -> None:
        category = str(metadata.get("classification") or self.classify(metadata))
        async with self._locks[category]:
            cases = await asyncio.to_thread(self._load_cases, category)
            if len(cases) < self.batch_size or len(cases) % self.batch_size:
                return
            await self._analyze(category, cases[-self.max_cases:])

    def _load_cases(self, category: str) -> list[dict]:
        cases = []
        for path in self.directory.glob("*.md"):
            try:
                _, header, body = path.read_text(encoding="utf-8").split("---", 2)
                item = yaml.safe_load(header) or {}
                if item.get("classification") == category:
                    cases.append({**item, "body": body.strip()})
            except (OSError, ValueError, yaml.YAMLError):
                continue
        return sorted(cases, key=lambda item: str(item.get("created", "")))

    async def _analyze(self, category: str, cases: list[dict]) -> None:
        evidence = [
            {
                "case_id": case.get("id", ""),
                "failure_kind": case.get("failure_kind", ""),
                "actor": case.get("actor", ""),
                "tool": case.get("tool", ""),
                "error": self._section(case.get("body", ""), "错误", 1200),
                "params": self._section(case.get("body", ""), "参数摘要", 500),
            }
            for case in cases
        ]
        prompt = f'''你是 Agent 运行质量分析器。只分析以下同类 Bad Case，不要调用工具、不要修改任何 Prompt、配置或代码。
类别：{category}
样本：{json.dumps(evidence, ensure_ascii=False)}
只输出 JSON：{{"summary":"一句话归因","root_causes":[{{"cause":"","confidence":0.0,"evidence_case_ids":[""]}}],"recommendations":[{{"priority":"high|medium|low","proposal":"可执行的改进建议","target":"prompt|tool|routing|validation|observability","evidence_case_ids":[""]}}],"needs_more_evidence":false}}。
要求：区分直接错误与推测；若样本不足或存在多种不相关原因，needs_more_evidence=true，禁止给出武断结论。建议仅供人工审核，不要宣称已实施。'''
        text = ""
        try:
            async for chunk in self.llm.chat(
                position="bad_case_analysis", messages=[{"role": "user", "content": prompt}],
                tools=None, stream=False, tag=":bad-case-analysis", max_tokens=2048,
            ):
                if chunk.type == "text_delta":
                    text += chunk.content
            data = self._json(text)
            if data:
                await asyncio.to_thread(self._write_analysis, category, cases, data)
        except Exception as exc:
            print(f"[bad_case] analysis failed for {category}: {exc}", flush=True)

    @staticmethod
    def _section(body: str, title: str, limit: int) -> str:
        match = re.search(rf"## {re.escape(title)}\s*(.*?)(?=\n## |\Z)", body, re.S)
        return (match.group(1).strip() if match else "")[:limit]

    @staticmethod
    def _json(text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            value = json.loads(clean[clean.find("{"):clean.rfind("}") + 1])
            return value if isinstance(value, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _write_analysis(self, category: str, cases: list[dict], data: dict) -> None:
        directory = self.directory / "analysis"
        directory.mkdir(parents=True, exist_ok=True)
        updated = datetime.now(timezone.utc).isoformat()
        case_ids = [str(case.get("id", "")) for case in cases if case.get("id")]
        header = {
            "type": "bad_case_analysis",
            "category": category,
            "updated": updated,
            "case_count": len(case_ids),
            "source_case_ids": case_ids,
            "status": "review_required",
            "needs_more_evidence": bool(data.get("needs_more_evidence", False)),
        }
        lines = ["---", yaml.safe_dump(header, allow_unicode=True, sort_keys=False).strip(), "---", "", f"# {category}", "", "## 摘要", str(data.get("summary", "")), "", "## 可能根因"]
        for item in data.get("root_causes", []):
            if isinstance(item, dict):
                lines.append(f"- {item.get('cause', '')}（置信度 {item.get('confidence', 0)}；证据：{', '.join(item.get('evidence_case_ids', []))}）")
        lines.extend(["", "## 待审核优化建议"])
        for item in data.get("recommendations", []):
            if isinstance(item, dict):
                lines.append(f"- [{item.get('priority', 'medium')}] {item.get('proposal', '')}（目标：{item.get('target', '')}；证据：{', '.join(item.get('evidence_case_ids', []))}）")
        if not data.get("root_causes"):
            lines.append("- 暂无可确认根因。")
        if not data.get("recommendations"):
            lines.append("- 暂无建议，等待更多同类样本。")
        (directory / f"{category}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
