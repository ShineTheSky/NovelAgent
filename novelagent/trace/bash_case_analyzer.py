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
    """Annotate each Bash case, then aggregate cases with the same LLM-defined purpose."""

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
            asyncio.create_task(self._annotate_and_analyze(payload))

    async def _annotate_and_analyze(self, payload: dict) -> None:
        annotation = await self._annotate_case(payload)
        if not annotation:
            return
        await asyncio.to_thread(self._update_case, payload, annotation)
        await self.analyze_when_ready(str(annotation["semantic_family"]))

    async def _annotate_case(self, payload: dict) -> dict:
        """Use one background LLM call to explain the purpose of this exact command."""
        sample = {
            "command": payload.get("command", ""),
            "working_dir": payload.get("working_dir", ""),
            "permission": payload.get("permission", ""),
            "outcome": payload.get("outcome", ""),
            "result_preview": str(payload.get("result_preview", ""))[:800],
            "error": str(payload.get("error", ""))[:800],
        }
        prompt = f'''你是 Bash 行为标注器。分析一条已脱敏的命令记录，判断它实际想完成什么，而不是只看命令首词。
记录：{json.dumps(sample, ensure_ascii=False)}
只输出 JSON：{{"purpose":"一句中文说明实际用途","semantic_family":"稳定的英文 snake_case 用途分类","suggested_tool":"Read|Grep|Glob|SearchRag|其他工具名|none","reason":"为何可或不可替代为专用工具","confidence":0.0}}。
复合命令要按整体目的分类；仅检查文件、版本、关键词、字数等属于读取/验证，不得误判为写入。semantic_family 必须只含小写字母、数字和下划线。'''
        text = ""
        try:
            async for chunk in self.llm.chat(
                position="bash_case_analysis", messages=[{"role": "user", "content": prompt}],
                tools=None, stream=False, tag=":bash-case-annotation", max_tokens=512,
            ):
                if chunk.type == "text_delta":
                    text += chunk.content
        except Exception as exc:
            print(f"[bash_case] annotation failed: {exc}", flush=True)
            return {}
        data = self._json(text)
        purpose = str(data.get("purpose", "")).strip()
        if not purpose:
            return {}
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "purpose": purpose,
            "semantic_family": self._semantic_family(data.get("semantic_family"), payload.get("command_family", "unknown")),
            "suggested_tool": str(data.get("suggested_tool", "none")).strip() or "none",
            "reason": str(data.get("reason", "")).strip(),
            "confidence": max(0.0, min(1.0, confidence)),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _semantic_family(value: object, fallback: object) -> str:
        family = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
        if family:
            return family[:80]
        fallback_family = re.sub(r"[^a-z0-9_]+", "_", str(fallback or "unknown").lower()).strip("_")
        return fallback_family[:80] or "unknown"

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
                annotation = item.get("purpose_analysis") or {}
                if annotation.get("semantic_family") == family:
                    cases.append(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(cases, key=lambda item: str(item.get("created_at", "")))

    async def _analyze(self, family: str, cases: list[dict]) -> None:
        samples = [{
            "case_id": item.get("id", ""), "command": item.get("command", ""),
            "purpose": (item.get("purpose_analysis") or {}).get("purpose", ""),
            "suggested_tool": (item.get("purpose_analysis") or {}).get("suggested_tool", "none"),
            "permission": item.get("permission", ""), "outcome": item.get("outcome", ""),
            "result_preview": str(item.get("result_preview", ""))[:500],
            "error": str(item.get("error", ""))[:500],
        } for item in cases]
        prompt = f'''你是工具演化分析器。下面是用途分类 `{family}` 的完整 Bash 行为样本，包含成功、失败、拦截和用户拒绝；不要把失败样本当成全部事实。
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
            "type": "bash_case_analysis", "semantic_family": family,
            "updated": datetime.now(timezone.utc).isoformat(), "case_count": len(case_ids),
            "source_case_ids": case_ids, "outcomes": dict(outcomes),
            "status": "review_required", "needs_more_evidence": bool(data.get("needs_more_evidence", False)),
        }
        lines = ["---", yaml.safe_dump(header, allow_unicode=True, sort_keys=False).strip(), "---", "", f"# Bash 用途分类：{family}", "", "## 观察总结", str(data.get("summary", "")), "", "## 使用模式", str(data.get("observed_pattern", "")), "", "## 风险"]
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

    def _update_case(self, payload: dict, annotation: dict) -> None:
        try:
            created = datetime.fromisoformat(str(payload["created_at"]))
            path = self.directory / created.strftime("%Y-%m-%d") / f"{payload['id']}.json"
            if not path.is_file():
                return
            case = json.loads(path.read_text(encoding="utf-8"))
            case["purpose_analysis"] = annotation
            path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[bash_case] annotation write failed: {exc}", flush=True)
