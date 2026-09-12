"""项目级 Evidence、Memory 与 Pattern 文件生命周期管理。"""

from pathlib import Path
from uuid import uuid4

import yaml

from novelagent.memory.auto_memory import AutoMemory
from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager
from novelagent.memory.prefetcher import Prefetcher


class MemoryManager:
    _KINDS = {"project_fact", "preference", "issue"}
    _SCOPES = {"global", "project", "arc", "chapter", "scene"}

    def __init__(self, working_dir: str, llm_client=None):
        self.working_dir = working_dir
        self.file_store = FileStore(working_dir)
        self.index_manager = IndexManager(self.file_store)
        self.prefetcher = Prefetcher(self.index_manager, self.file_store, llm_client)
        self.auto_memory = AutoMemory(self.index_manager, self.file_store, llm_client)

    def init_project_memory(self) -> None:
        memory_dir = Path(self.working_dir) / ".memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        for subdirectory in ("evidence", "memory", "pattern", "user", "feedback", "reference", "project"):
            (memory_dir / subdirectory).mkdir(exist_ok=True)
        self.index_manager.rebuild()

    async def on_user_input(self, user_message: str) -> list[str] | None:
        return await self.prefetcher.fetch(user_message)

    def list_records(self, layer: str) -> list[dict]:
        prefix = f".memory/{layer}/"
        records = [entry for entry in self.file_store.scan_frontmatter() if str(entry.get("_path", "")).startswith(prefix)]
        records.sort(key=lambda entry: str(entry.get("updated", entry.get("created", ""))), reverse=True)
        return records

    def get_record(self, layer: str, record_id: str) -> dict | None:
        id_key = {"evidence": "evidence_id", "memory": "memory_id", "pattern": "pattern_id"}.get(layer)
        if not id_key:
            return None
        return next((entry for entry in self.list_records(layer) if entry.get(id_key) == record_id), None)

    def persist_evidence(self, trace_id: str, items: list[dict]) -> list[dict]:
        written = []
        for item in items:
            evidence_id = f"evd_{uuid4().hex}"
            metadata = self._record_metadata(item, trace_id, "evidence", evidence_id=evidence_id)
            self._write_record("evidence", evidence_id, metadata, item)
            written.append({**metadata, "file_path": f".memory/evidence/{evidence_id}.md"})
        if written:
            self.rebuild_index()
        return written

    def persist_memories(self, trace_id: str, items: list[dict]) -> list[dict]:
        written = []
        for item in items:
            memory_id = f"mem_{uuid4().hex}"
            metadata = self._record_metadata(item, trace_id, "active", memory_id=memory_id)
            self._write_record("memory", memory_id, metadata, item)
            written.append({**metadata, "file_path": f".memory/memory/{memory_id}.md"})
        if written:
            self.rebuild_index()
        return written

    def promote_evidence(self, evidence_id: str) -> dict | None:
        evidence = self.get_record("evidence", evidence_id)
        if not evidence:
            return None
        trace_id = str(evidence.get("trace_id", ""))
        if not trace_id:
            return None
        memory_id = f"mem_{uuid4().hex}"
        metadata = dict(evidence)
        metadata.pop("evidence_id", None)
        metadata.pop("_path", None)
        metadata.update({
            "memory_id": memory_id,
            "status": "active",
            "summary": str(evidence.get("summary", "")),
            "origin": "trace",
        })
        body = self._body_from_record(evidence)
        self._write_content("memory", memory_id, metadata, body)
        self.file_store.delete(str(evidence["_path"]).removeprefix(".memory/"))
        self.rebuild_index()
        return {**metadata, "file_path": f".memory/memory/{memory_id}.md"}

    def create_or_update_pattern(self, item: dict) -> dict:
        pattern_id = str(item.get("pattern_id", ""))
        existing = self.get_record("pattern", pattern_id) if pattern_id else None
        if not pattern_id or existing is None:
            pattern_id = f"pat_{uuid4().hex}"
            existing = {}
        memory_links = item.get("memory_links", [])
        supporting = [link["memory_id"] for link in memory_links if link.get("relation") == "support"]
        contradicting = [link["memory_id"] for link in memory_links if link.get("relation") == "contradict"]
        trace_ids = list(dict.fromkeys(str(trace_id) for trace_id in item.get("trace_ids", []) if trace_id))
        metadata = {
            **{key: value for key, value in existing.items() if key != "_path"},
            "type": "pattern",
            "pattern_id": pattern_id,
            "status": str(item.get("status", existing.get("status", "ready_for_review"))),
            "kind": str(item.get("kind", existing.get("kind", "preference"))),
            "subtype": str(item.get("subtype", existing.get("subtype", ""))),
            "dimension": str(item.get("dimension", existing.get("dimension", ""))),
            "scope": str(item.get("scope", existing.get("scope", "project"))),
            "confidence": float(item.get("confidence", existing.get("confidence", 0.5))),
            "claim": str(item.get("claim", existing.get("claim", ""))),
            "summary": str(item.get("summary", item.get("claim", existing.get("summary", ""))))[:500],
            "supporting_memory_ids": list(dict.fromkeys(supporting)),
            "contradicting_memory_ids": list(dict.fromkeys(contradicting)),
            "trace_ids": trace_ids,
            "support_count": len(set(supporting)),
            "contradiction_count": len(set(contradicting)),
            "tags": ["pattern", str(item.get("kind", "preference"))],
            "origin": "trace",
        }
        body = str(item.get("content") or metadata["claim"])
        self._write_content("pattern", pattern_id, metadata, body)
        self.rebuild_index()
        return {**metadata, "file_path": f".memory/pattern/{pattern_id}.md"}

    def render_confirmed_patterns(self, limit: int = 8) -> str:
        patterns = [record for record in self.list_records("pattern") if record.get("status") == "confirmed"][:limit]
        return "\n".join(f"- {pattern.get('claim') or pattern.get('summary')}" for pattern in patterns)

    def rebuild_index(self) -> str:
        return self.index_manager.rebuild()

    def get_index_content(self) -> str:
        return self.index_manager.get_content()

    def _record_metadata(self, item: dict, trace_id: str, status: str, **identifier: str) -> dict:
        kind = str(item.get("kind", ""))
        if kind not in self._KINDS:
            raise ValueError("无效记忆类型")
        scope = str(item.get("scope", "project"))
        if scope not in self._SCOPES:
            scope = "project"
        claim = str(item["claim"]).strip()[:1000]
        if not claim:
            raise ValueError("记忆结论不能为空")
        return {
            "type": kind,
            "subtype": str(item.get("subtype", ""))[:100],
            "tags": ["trace", kind],
            "summary": str(item.get("summary", "") or claim)[:500],
            "claim": claim,
            "status": status,
            "origin": "trace",
            **identifier,
            "trace_id": trace_id,
            "source_event_ids": list(dict.fromkeys(str(value) for value in item.get("source_event_ids", []) if value)),
            "scope": scope,
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.5)))),
        }

    def _write_record(self, layer: str, record_id: str, metadata: dict, item: dict) -> None:
        body = str(item.get("content") or item["claim"]).strip()
        self._write_content(layer, record_id, metadata, body)

    def _write_content(self, layer: str, record_id: str, metadata: dict, body: str) -> None:
        trace_id = metadata.get("trace_id", "")
        event_lines = "\n".join(f"- `{event_id}`" for event_id in metadata.get("source_event_ids", []))
        trace_section = f"\n\n## Trace 溯源\n- Trace：`{trace_id}`\n{event_lines}" if trace_id else ""
        if layer == "pattern":
            links = [*metadata.get("supporting_memory_ids", []), *metadata.get("contradicting_memory_ids", [])]
            trace_section += "\n\n## 关联来源\n" + "\n".join(f"- Memory：`{memory_id}`" for memory_id in links)
            trace_section += "\n" + "\n".join(f"- Trace：`{trace_id}`" for trace_id in metadata.get("trace_ids", []))
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        self.file_store.write(f"{layer}/{record_id}.md", f"---\n{frontmatter}\n---\n\n{body}{trace_section}")

    def _body_from_record(self, record: dict) -> str:
        path = str(record.get("_path", "")).removeprefix(".memory/")
        text = self.file_store.read(path)
        return text.split("---", 2)[-1].strip().split("## Trace 溯源", 1)[0].strip()

    # Compatibility for the previous Trace → .memory/ implementation.
    def persist_trace_memories(self, trace_id: str, memories: list[dict]) -> list[dict]:
        return self.persist_memories(trace_id, memories)


class PatternContextProvider:
    """只注入已确认 Pattern；Evidence 和待审核 Pattern 不影响创作上下文。"""

    def __init__(self, working_dir: str):
        self.working_dir = working_dir

    async def render(self, project_id: str) -> str:
        return MemoryManager(str(Path(self.working_dir) / project_id)).render_confirmed_patterns()
