"""Mirror trace-backed lifecycle memories into project memory files."""

from pathlib import Path

from novelagent.memory.file_store import FileStore


class TraceMemoryMaterializer:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

    def sync(self, memory: dict) -> str:
        """Write a snapshot source.  A Trace-status item stays on disk but is not indexed."""
        project_dir = self.workspace_dir / memory["project_id"]
        store = FileStore(str(project_dir))
        status = str(memory.get("status", "memory"))
        path = f"trace/{memory['memory_id']}.md"
        tags = [str(memory.get("kind", "memory"))]
        if memory.get("subtype"):
            tags.append(str(memory["subtype"]))
        content = f"""---
type: trace_{status}
tags: {tags}
summary: {memory.get('claim', '')}
weight: {memory.get('importance', 0)}
created: {memory.get('created_at', '')}
updated: {memory.get('last_reinforced_at') or memory.get('created_at', '')}
status: {'trace' if status == 'trace' else 'active'}
trace_id: {memory.get('trace_id', '')}
memory_id: {memory.get('memory_id', '')}
---

{memory.get('claim', '')}
"""
        store.write(path, content)
        return f".memory/{path}"
