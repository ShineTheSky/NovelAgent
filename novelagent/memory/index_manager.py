"""memory.md 索引生成与更新。"""

from novelagent.memory.file_store import FileStore


class IndexManager:
    MAX_ENTRIES = 200
    _STATUS_PRIORITY = {"confirmed": 0, "active": 1, "ready_for_review": 2, "pending": 3, "candidate": 4, "disputed": 5}

    def __init__(self, file_store: FileStore):
        self.file_store = file_store
        self.memory_dir = file_store.memory_dir

    def rebuild(self) -> str:
        entries = self.file_store.scan_frontmatter()
        entries.sort(key=lambda entry: (self._STATUS_PRIORITY.get(str(entry.get("status", "")), 9), str(entry.get("updated", ""))), reverse=False)
        entries.sort(key=lambda entry: str(entry.get("updated", "")), reverse=True)
        entries.sort(key=lambda entry: self._STATUS_PRIORITY.get(str(entry.get("status", "")), 9))

        recent = entries[:self.MAX_ENTRIES]
        archived = entries[self.MAX_ENTRIES:]
        if archived:
            archive_lines = ["# 记忆归档", ""]
            for entry in archived:
                archive_lines.append(f"- [{entry.get('type', '?')}/{entry.get('status', '?')}] {entry.get('summary', '')} → {entry.get('_path', '')}")
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            (self.memory_dir / "memory_archive.md").write_text("\n".join(archive_lines), encoding="utf-8")

        lines = [
            "# 记忆索引",
            "",
            f"> 最近 {len(recent)} 条记忆、证据与模式。完整内容请读取对应文件。",
            "",
            "| # | 类型 | 状态 | 标签 | 摘要 | Trace | 文件路径 |",
            "|---|------|------|------|------|-------|----------|",
        ]
        for index, entry in enumerate(recent, 1):
            tags = ", ".join(entry.get("tags", [])) if entry.get("tags") else "-"
            trace_id = str(entry.get("trace_id", ""))[:18] or "-"
            lines.append(
                f"| {index} | {entry.get('type', '?')} | {entry.get('status', '?')} | {tags} | "
                f"{entry.get('summary', '')} | {trace_id} | {entry.get('_path', '')} |"
            )
        if not recent:
            lines.append("| - | - | - | - | 暂无记忆 | - | - |")

        content = "\n".join(lines)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "memory.md").write_text(content, encoding="utf-8")
        return content

    def get_content(self) -> str:
        index_path = self.memory_dir / "memory.md"
        return index_path.read_text(encoding="utf-8") if index_path.exists() else ""
