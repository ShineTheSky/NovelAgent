"""Batch analysis for global Agent Bad Cases; never modifies prompts or tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from novelagent.trace.agent_run import AgentRunTrace


class BadCaseAnalyzer:
    """Classify raw failures, then periodically derive review-only diagnoses."""

    def __init__(self, llm_client, workspace_dir: str, config: dict | None = None, trace_store=None):
        settings = config or {}
        self.llm = llm_client
        self.directory = Path(workspace_dir).resolve().parent / "data" / "agent_bad_cases"
        self.enabled = bool(settings.get("enabled", True))
        self.batch_size = max(2, int(settings.get("batch_size", 3)))
        self.max_cases = max(self.batch_size, int(settings.get("max_cases", 12)))
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.trace_store = trace_store
        self.bad_case_recorder = None

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
        if self.trace_store:
            for item, case in zip(evidence, cases):
                source_trace_id = str(case.get("source_trace_id", ""))
                item["source_trace_id"] = source_trace_id
                item["trace_events"] = await self._load_trace_context(source_trace_id)
        prompt = f'''你是 Agent 运行质量分析器。只分析以下同类 Bad Case，不要调用工具、不要修改任何 Prompt、配置或代码。
类别：{category}
样本：{json.dumps(evidence, ensure_ascii=False)}
只输出 JSON：{{"summary":"一句话归因","root_causes":[{{"cause":"","confidence":0.0,"evidence_case_ids":[""]}}],"recommendations":[{{"priority":"high|medium|low","proposal":"可执行的改进建议","target":"prompt|tool|routing|validation|observability","evidence_case_ids":[""]}}],"needs_more_evidence":false}}。
要求：trace_events 是程序根据 source_trace_id 从独立 Agent Trace 中读取的执行过程，不是 Bad Case 摘要的复制。区分直接错误与推测；若样本不足或存在多种不相关原因，needs_more_evidence=true，禁止给出武断结论。建议仅供人工审核，不要宣称已实施。'''
        text = ""
        agent_trace = None
        try:
            if self.trace_store:
                agent_trace = await AgentRunTrace.try_start(
                    self.trace_store, session_id="bad-case-analysis", project_id="__agent_bad_cases__",
                    actor="bad_case_analyzer", position="bad_case_analysis",
                    title=f"[bad_case_analyzer] {category}",
                )
                agent_trace.add_request(
                    position="bad_case_analysis", tag=":bad-case-analysis",
                    messages=[{"role": "user", "content": prompt}], tools=None,
                )
            async for chunk in self.llm.chat(
                position="bad_case_analysis", messages=[{"role": "user", "content": prompt}],
                tools=None, stream=False, tag=":bad-case-analysis", max_tokens=2048,
            ):
                if chunk.type == "text_delta":
                    text += chunk.content
                    if agent_trace:
                        agent_trace.add("text_delta", {"content": chunk.content})
                elif chunk.type == "thinking" and agent_trace:
                    agent_trace.add("thinking", {"content": chunk.content})
                elif chunk.type == "error":
                    if agent_trace:
                        agent_trace.add("error", {"message": chunk.error})
                    raise RuntimeError(chunk.error or "Bad Case 分析调用失败")
            data = self._json(text)
            if not data:
                raise ValueError("Bad Case 分析未返回有效 JSON")
            if agent_trace:
                await agent_trace.finish("completed", text)
            await asyncio.to_thread(self._write_analysis, category, cases, data)
        except Exception as exc:
            print(f"[bad_case] analysis failed for {category}: {exc}", flush=True)
            if agent_trace:
                await agent_trace.finish("failed", error=str(exc))
            if self.bad_case_recorder:
                await self.bad_case_recorder.capture(
                    source_trace_id=agent_trace.trace_id if agent_trace else "",
                    session_id="bad-case-analysis", project_id="__agent_bad_cases__",
                    failure_kind="bad_case_analysis_failure", actor="bad_case_analyzer",
                    error=str(exc), messages=[{"role": "user", "content": prompt}],
                    schedule_analysis=False,
                )

    async def _load_trace_context(self, trace_id: str) -> list[dict]:
        if not trace_id:
            return []
        events = await self.trace_store.list_events(trace_id, limit=1_000)
        result = []
        for event in events:
            payload = event.get("payload") or {}
            item = {
                "sequence": event.get("sequence_no", 0),
                "type": event.get("event_type", ""),
                "actor": event.get("actor", ""),
            }
            for key in ("content", "message", "tool", "params", "data", "error", "success", "position", "tag"):
                if key in payload:
                    value = payload[key]
                    item[key] = str(value)[:2_000] if isinstance(value, str) else value
            result.append(item)
        return result

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
