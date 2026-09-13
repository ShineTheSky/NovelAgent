"""Global, file-backed records for Agent and tool failures."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from novelagent.trace.recorder import sanitize_payload
from novelagent.trace.store import TraceStore


GLOBAL_PROJECT_ID = "__agent_bad_cases__"


class AgentBadCaseRecorder:
    """Capture failures without affecting project memories or the user-facing turn."""

    def __init__(self, store: TraceStore, workspace_dir: str, analyzer=None):
        self.store = store
        self.directory = Path(workspace_dir).resolve().parent / "data" / "agent_bad_cases"
        self.analyzer = analyzer

    @staticmethod
    def _snapshot(messages: list[dict] | None) -> list[dict]:
        snapshot = []
        for message in (messages or [])[-8:]:
            if not isinstance(message, dict):
                continue
            snapshot.append({
                "role": str(message.get("role", "")),
                "content": str(message.get("content", ""))[:2_000],
                "tool_calls": message.get("tool_calls", []),
            })
        return sanitize_payload(snapshot)

    async def capture(
        self,
        *,
        source_trace_id: str,
        session_id: str,
        project_id: str,
        failure_kind: str,
        actor: str,
        error: str,
        tool: str = "",
        params: dict | None = None,
        messages: list[dict] | None = None,
        duration_ms: float | None = None,
    ) -> str:
        """Persist a separate bad-case trace and review file. Never raises to the caller."""
        try:
            title = f"[Agent bad case] {failure_kind}: {tool or actor}"
            trace_id = await self.store.create_trace(session_id, GLOBAL_PROJECT_ID, title)
            await self.store.set_trace_operation(trace_id, "agent_bad_case", "skipped")
            metadata = sanitize_payload({
                "source_trace_id": source_trace_id,
                "source_project_id": project_id,
                "source_session_id": session_id,
                "failure_kind": failure_kind,
                "actor": actor,
                "tool": tool,
                "params": params or {},
                "error": error,
            })
            metadata["classification"] = self.analyzer.classify(metadata) if self.analyzer else "unclassified"
            await self.store.append_event(trace_id, 1, "bad_case_context", "system", metadata)
            await self.store.append_event(trace_id, 2, "error", actor, {
                "message": str(error)[:16_000],
                "tool": tool,
                "failure_kind": failure_kind,
            }, duration_ms=duration_ms)
            await self.store.append_event(trace_id, 3, "context_snapshot", "system", {
                "messages": self._snapshot(messages),
            })
            await self.store.finish_trace(trace_id, "failed", str(error)[:16_000])
            await asyncio.to_thread(self._write_file, trace_id, metadata, self._snapshot(messages))
            if self.analyzer:
                self.analyzer.schedule(trace_id, metadata)
            return trace_id
        except Exception as exc:
            print(f"[agent_bad_case] capture failed: {exc}", flush=True)
            return ""

    def _write_file(self, trace_id: str, metadata: dict, snapshot: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        created = datetime.now(timezone.utc).isoformat()
        lines = [
            "---",
            f"id: {trace_id}",
            "type: agent_bad_case",
            f"created: {created}",
            f"failure_kind: {metadata.get('failure_kind', '')}",
            f"classification: {metadata.get('classification', 'unclassified')}",
            f"actor: {metadata.get('actor', '')}",
            f"tool: {metadata.get('tool', '')}",
            f"source_trace_id: {metadata.get('source_trace_id', '')}",
            f"source_project_id: {metadata.get('source_project_id', '')}",
            f"source_session_id: {metadata.get('source_session_id', '')}",
            "---",
            "",
            "## 错误",
            str(metadata.get("error", "")),
            "",
            "## 参数摘要",
            repr(metadata.get("params", {})),
            "",
            "## 最近上下文摘要",
        ]
        for message in snapshot:
            lines.append(f"### {message.get('role', 'unknown')}")
            lines.append(str(message.get("content", "")))
            lines.append("")
        stamp = created.replace(":", "-").replace("+", "_")
        (self.directory / f"{stamp}_{trace_id}.md").write_text("\n".join(lines), encoding="utf-8")
