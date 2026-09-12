"""memory.md 索引生成与更新"""

from pathlib import Path
from novelagent.memory.file_store import FileStore


class IndexManager:
    MAX_ENTRIES = 200

    def __init__(self, file_store: FileStore):
        self.file_store = file_store
        self.memory_dir = file_store.memory_dir

    def rebuild(self, exclude_prefixes: tuple[str, ...] = (), additional_entries: list[dict] | None = None) -> str:
        entries = self.file_store.scan_frontmatter(exclude_prefixes)
        entries.extend(additional_entries or [])

        # memory.md is a sliding window: newer days win; within a day, heavier
        # memories win.  Source files stay intact when they fall outside it.
        def sort_key(entry: dict) -> tuple[str, float]:
            date = str(entry.get("updated") or entry.get("created") or "")[:10]
            try:
                weight = float(entry.get("weight", 50))
            except (TypeError, ValueError):
                weight = 50.0
            return date, weight

        entries.sort(key=sort_key, reverse=True)

        # Keep top 200
        recent = entries[:self.MAX_ENTRIES]
        archived = entries[self.MAX_ENTRIES:]

        # Build memory.md
        lines = [
            "# 记忆索引",
            "",
            f"> 可用记忆的滑动窗口快照：显示 {len(recent)} / {len(entries)} 条。按更新时间（天）倒序；同一天按权重倒序。",
            "",
            "| # | 类型 | 标签 | 权重 | 更新日期 | 摘要 | 文件路径 |",
            "|---|------|------|------|----------|------|----------|",
        ]

        for i, entry in enumerate(recent, 1):
            entry_type = entry.get("type", "?")
            tags = ", ".join(entry.get("tags", [])) if entry.get("tags") else "-"
            summary = entry.get("summary", "")
            path = entry.get("_path", "")
            weight = entry.get("weight", 50)
            date = str(entry.get("updated") or entry.get("created") or "")[:10] or "-"
            lines.append(f"| {i} | {entry_type} | {tags} | {weight} | {date} | {summary} | {path} |")

        if not recent:
            lines.append("| - | - | 暂无记忆 | - | - | - | - |")

        content = "\n".join(lines)
        index_path = self.memory_dir / "memory.md"
        index_path.write_text(content, encoding="utf-8")
        return content

    def get_content(self) -> str:
        index_path = self.memory_dir / "memory.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return ""
