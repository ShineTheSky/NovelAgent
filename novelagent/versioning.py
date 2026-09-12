"""Revision metadata for user-facing Markdown artifacts.

The file header is the source of truth.  SQLite traces may refer to a revision,
but a copied project directory remains independently auditable.
"""

import asyncio
import hashlib
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_MANAGED_KEYS = {
    "revision_id", "parent_revision_id", "updated_at", "updated_by",
    "operation_id", "body_sha256",
}


@dataclass
class RevisionInfo:
    revision_id: str
    body: str
    metadata: dict
    legacy: bool = False


class RevisionConflict(Exception):
    def __init__(self, info: RevisionInfo):
        self.info = info
        super().__init__(f"修订冲突：文件当前版本为 {info.revision_id}")


class RevisionManager:
    """Serializes writes to a file and atomically updates its YAML frontmatter."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def is_managed(path: Path, working_dir: str) -> bool:
        try:
            relative = path.resolve().relative_to(Path(working_dir).resolve())
        except ValueError:
            return False
        # Memory internals are maintained by the memory subsystem, not creative agents.
        return path.suffix.lower() == ".md" and ".memory" not in relative.parts

    @staticmethod
    def _legacy_id(body: str) -> str:
        return f"legacy_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def parse(content: str) -> RevisionInfo:
        match = _FRONTMATTER.match(content)
        if not match:
            return RevisionInfo(RevisionManager._legacy_id(content), content, {}, legacy=True)
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            # A malformed header is still protected by a stable content-derived revision.
            return RevisionInfo(RevisionManager._legacy_id(content), content, {}, legacy=True)
        if not isinstance(metadata, dict) or not metadata.get("revision_id"):
            return RevisionInfo(RevisionManager._legacy_id(content), content, {}, legacy=True)
        return RevisionInfo(str(metadata["revision_id"]), content[match.end():], metadata)

    @staticmethod
    def _body_from_candidate(content: str) -> tuple[dict, str]:
        """Ignore agent-supplied revision fields while preserving user metadata."""
        match = _FRONTMATTER.match(content)
        if not match:
            return {}, content
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}, content
        if not isinstance(metadata, dict):
            metadata = {}
        return {key: value for key, value in metadata.items() if key not in _MANAGED_KEYS}, content[match.end():]

    @staticmethod
    def render(metadata: dict, body: str) -> str:
        header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{header}\n---\n{body}"

    @staticmethod
    def _new_revision_id(actor: str) -> str:
        safe_actor = re.sub(r"[^a-zA-Z0-9_-]", "_", actor or "agent")[:32]
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f")[:-3]
        offset = datetime.now().astimezone().strftime("%z")
        return f"rev_{timestamp}{offset}_{safe_actor}_{secrets.token_hex(4)}"

    async def commit(
        self, path: Path, content: str, expected_revision_id: str | None,
        *, actor: str, operation_id: str,
    ) -> RevisionInfo:
        key = str(path.resolve()).lower()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            current = self.parse(existing) if existing is not None else None
            expected = (expected_revision_id or "").strip()
            if current is not None and expected != current.revision_id:
                raise RevisionConflict(current)
            if current is None and expected not in {"", "new"}:
                raise RevisionConflict(RevisionInfo("new", "", {}, legacy=False))

            supplied_metadata, body = self._body_from_candidate(content)
            metadata = dict(current.metadata) if current and not current.legacy else {}
            metadata.update(supplied_metadata)
            revision_id = self._new_revision_id(actor)
            metadata.update({
                "revision_id": revision_id,
                "parent_revision_id": current.revision_id if current else None,
                "updated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "updated_by": actor or "agent",
                "operation_id": operation_id or "",
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            })
            rendered = self.render(metadata, body)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(rendered)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return RevisionInfo(revision_id, body, metadata)


revision_manager = RevisionManager()
