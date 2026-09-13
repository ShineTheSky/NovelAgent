"""Global, redacted Bash execution cases for future monitored-tool design."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization)"
    r"\s*(?:=|:)\s*([^\s;|]+)"
)
_SECRET_FLAG = re.compile(r"(?i)(--(?:api[-_]?key|token|password|secret))\s+([^\s;|]+)")


def redact_shell_text(value: object, limit: int = 4_000) -> str:
    """Keep a useful command/result sample without retaining inline credentials."""
    text = str(value or "")
    text = _INLINE_SECRET.sub(r"\1=[REDACTED]", text)
    text = _SECRET_FLAG.sub(r"\1 [REDACTED]", text)
    return text[:limit] + ("\n[TRUNCATED]" if len(text) > limit else "")


class BashCaseRecorder:
    """One JSON case per Bash request, intentionally outside project memory."""

    def __init__(self, workspace_dir: str, analyzer=None):
        self.directory = Path(workspace_dir).resolve().parent / "data" / "bash_cases"
        self.analyzer = analyzer

    async def capture(
        self,
        *,
        session_id: str,
        project_id: str,
        source_trace_id: str,
        actor: str,
        operation_id: str,
        params: dict | None = None,
        permission: str,
        status: str,
        success: bool | None = None,
        result: object = "",
        error: object = "",
        duration_ms: float | None = None,
    ) -> None:
        try:
            command = redact_shell_text((params or {}).get("command", ""))
            working_dir = redact_shell_text((params or {}).get("working_dir", ""), limit=500)
            now = datetime.now(timezone.utc)
            outcome = "succeeded" if status == "executed" and success else "failed" if status == "executed" else status
            payload = {
                "id": f"bash_{uuid.uuid4().hex}",
                "type": "bash_tool_case",
                "case_kind": "bash_command",
                "created_at": now.isoformat(),
                "session_id": session_id,
                "project_id": project_id,
                "source_trace_id": source_trace_id,
                "actor": actor,
                "operation_id": operation_id,
                "command": command,
                "command_family": self.analyzer.classify(command) if self.analyzer else "unclassified",
                "working_dir": working_dir,
                "permission": permission,
                "status": status,
                "outcome": outcome,
                "success": success,
                "result_preview": redact_shell_text(result),
                "error": redact_shell_text(error),
                "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            }
            await asyncio.to_thread(self._write, now, payload)
            if self.analyzer:
                self.analyzer.schedule(payload)
        except Exception as exc:
            print(f"[bash_case] capture failed: {exc}", flush=True)

    def _write(self, now: datetime, payload: dict) -> None:
        directory = self.directory / now.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{payload['id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
