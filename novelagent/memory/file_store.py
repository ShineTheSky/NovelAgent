""".memory/ 文件读写"""

import yaml
from pathlib import Path
from datetime import datetime, timezone


class FileStore:
    def __init__(self, working_dir: str):
        self.memory_dir = Path(working_dir) / ".memory"

    def _ensure_dir(self):
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def read(self, rel_path: str) -> str:
        file_path = self.memory_dir / rel_path
        if not file_path.exists():
            return ""
        return file_path.read_text(encoding="utf-8")

    def write(self, rel_path: str, content: str):
        self._ensure_dir()
        file_path = self.memory_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        # If file exists, preserve original created time
        existing = {}
        if file_path.exists():
            existing_text = file_path.read_text(encoding="utf-8")
            if existing_text.startswith("---"):
                parts = existing_text.split("---", 2)
                if len(parts) >= 3:
                    try:
                        existing = yaml.safe_load(parts[1]) or {}
                    except yaml.YAMLError:
                        pass

        # Build frontmatter
        frontmatter = {
            "type": existing.get("type", ""),
            "tags": existing.get("tags", []),
            "summary": existing.get("summary", ""),
            "created": existing.get("created", now),
            "updated": now,
            "status": existing.get("status", "active"),
        }

        # Extract frontmatter from new content if provided
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    new_fm = yaml.safe_load(parts[1]) or {}
                    frontmatter.update({k: v for k, v in new_fm.items() if v})
                    body = parts[2].strip()
                except yaml.YAMLError:
                    pass

        fm_yaml = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        file_content = f"---\n{fm_yaml}\n---\n\n{body}"
        file_path.write_text(file_content, encoding="utf-8")

    def scan_frontmatter(self) -> list[dict]:
        """扫描所有.md文件，提取frontmatter元信息"""
        self._ensure_dir()
        results = []
        for md_file in self.memory_dir.rglob("*.md"):
            if md_file.name == "memory.md" or md_file.name == "memory_archive.md":
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        rel = str(md_file.relative_to(self.memory_dir)).replace("\\", "/")
                        fm["_path"] = ".memory/" + rel
                        results.append(fm)
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
        return results
