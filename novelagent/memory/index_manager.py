"""memory.md 索引生成与更新"""

from pathlib import Path
from novelagent.memory.file_store import FileStore


class IndexManager:
    MAX_ENTRIES = 200

    def __init__(self, file_store: FileStore):
        self.file_store = file_store
        self.memory_dir = file_store.memory_dir

    def rebuild(self) -> str:
        entries = self.file_store.scan_frontmatter()

        # Sort by updated time descending
        entries.sort(key=lambda e: e.get("updated", ""), reverse=True)

        # Keep top 200
        recent = entries[:self.MAX_ENTRIES]
        archived = entries[self.MAX_ENTRIES:]

        # Archive old entries
        if archived:
            archive_path = self.memory_dir / "memory_archive.md"
            archive_lines = ["# 记忆归档\n"]
            for e in archived:
                archive_lines.append(f"- [{e.get('type', '?')}] {e.get('summary', '')} → {e.get('_path', '')}")
            archive_path.write_text("\n".join(archive_lines), encoding="utf-8")

        # Build memory.md
        lines = [
            "# 记忆索引",
            "",
            f"> 最近{len(recent)}条记忆的元信息摘要。每条记忆的完整内容请用Grep或Read查看对应文件。",
            "",
            "| # | 类型 | 标签 | 摘要 | 文件路径 |",
            "|---|------|------|------|----------|",
        ]

        for i, entry in enumerate(recent, 1):
            entry_type = entry.get("type", "?")
            tags = ", ".join(entry.get("tags", [])) if entry.get("tags") else "-"
            summary = entry.get("summary", "")
            path = entry.get("_path", "")
            lines.append(f"| {i} | {entry_type} | {tags} | {summary} | {path} |")

        if not recent:
            lines.append("| - | - | 暂无记忆 | - | - |")

        content = "\n".join(lines)
        index_path = self.memory_dir / "memory.md"
        index_path.write_text(content, encoding="utf-8")
        return content

    def get_content(self) -> str:
        index_path = self.memory_dir / "memory.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return ""
