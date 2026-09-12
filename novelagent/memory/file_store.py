""".memory/ 文件读写。"""

from datetime import datetime, timezone
from pathlib import Path
import shutil

import yaml


class FileStore:
    def __init__(self, working_dir: str):
        self.memory_dir = Path(working_dir) / ".memory"

    def _ensure_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def resolve_safe(self, rel_path: str) -> Path:
        self._ensure_dir()
        target = (self.memory_dir / rel_path).resolve()
        root = self.memory_dir.resolve()
        if target != root and root not in target.parents:
            raise ValueError("记忆路径超出 .memory 目录")
        return target

    def exists(self, rel_path: str) -> bool:
        return self.resolve_safe(rel_path).exists()

    def read(self, rel_path: str) -> str:
        file_path = self.resolve_safe(rel_path)
        if not file_path.exists() or not file_path.is_file():
            return ""
        return file_path.read_text(encoding="utf-8")

    def read_frontmatter(self, rel_path: str) -> dict:
        text = self.read(rel_path)
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            data = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, rel_path: str, content: str) -> None:
        file_path = self.resolve_safe(rel_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        existing = self.read_frontmatter(rel_path) if file_path.exists() else {}
        frontmatter = dict(existing)
        frontmatter.setdefault("type", "")
        frontmatter.setdefault("tags", [])
        frontmatter.setdefault("summary", "")
        frontmatter.setdefault("created", now)
        frontmatter.setdefault("status", "active")
        frontmatter["updated"] = now

        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    new_fm = yaml.safe_load(parts[1]) or {}
                    if isinstance(new_fm, dict):
                        frontmatter.update({key: value for key, value in new_fm.items() if value is not None and value != ""})
                    body = parts[2].strip()
                except yaml.YAMLError:
                    pass

        fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        file_path.write_text(f"---\n{fm_yaml}\n---\n\n{body}", encoding="utf-8")

    def delete(self, rel_path: str) -> None:
        file_path = self.resolve_safe(rel_path)
        if file_path.exists():
            file_path.unlink()

    def move(self, source_rel_path: str, target_rel_path: str) -> None:
        source = self.resolve_safe(source_rel_path)
        target = self.resolve_safe(target_rel_path)
        if not source.exists():
            raise FileNotFoundError(source_rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def scan_frontmatter(self) -> list[dict]:
        """扫描所有 Markdown 文件，提取 frontmatter 元信息。"""
        self._ensure_dir()
        results = []
        for md_file in self.memory_dir.rglob("*.md"):
            if md_file.name in {"memory.md", "memory_archive.md"}:
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                if not text.startswith("---"):
                    continue
                parts = text.split("---", 2)
                if len(parts) < 3:
                    continue
                frontmatter = yaml.safe_load(parts[1]) or {}
                if not isinstance(frontmatter, dict):
                    continue
                rel = str(md_file.relative_to(self.memory_dir)).replace("\\", "/")
                frontmatter["_path"] = ".memory/" + rel
                results.append(frontmatter)
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
        return results
