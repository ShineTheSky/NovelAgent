"""Complete, project-local revisions and their original evidence.

The database is authoritative.  The Markdown file is a materialized current
revision and can be repaired after a crash between the database commit and
the atomic file replacement.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


class HistoryConflict(Exception):
    pass


class DocumentHistoryStore:
    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).resolve()
        self.db_path = self.project_dir / ".history" / "history.sqlite3"

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                latest_revision_id TEXT
            );
            CREATE TABLE IF NOT EXISTS revisions (
                revision_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                version_no INTEGER NOT NULL,
                parent_revision_id TEXT,
                body_md TEXT NOT NULL,
                rendered_md TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(document_id, version_no)
            );
            CREATE INDEX IF NOT EXISTS idx_history_revision_document
                ON revisions(document_id, version_no DESC);
            CREATE TABLE IF NOT EXISTS dependencies (
                revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                path TEXT NOT NULL,
                dependency_revision_id TEXT NOT NULL,
                PRIMARY KEY(revision_id, path)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(revision_id, kind, run_id)
            );
            CREATE TABLE IF NOT EXISTS no_change (
                decision_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                opinion TEXT NOT NULL,
                rationale TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_dir).as_posix()
        except ValueError as exc:
            raise ValueError("History path escapes project") from exc

    @staticmethod
    def _digest(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _replace(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def recover_file(self, path: Path) -> None:
        """Materialize a committed revision, but never overwrite external edits."""
        if not self.db_path.exists():
            return
        relative = self._relative(path)
        with self._connect() as db:
            latest = db.execute("""
                SELECT r.* FROM documents d JOIN revisions r
                    ON r.revision_id = d.latest_revision_id
                WHERE d.path = ?
            """, (relative,)).fetchone()
            if latest is None:
                return
            actual = path.read_text(encoding="utf-8") if path.exists() else None
            if actual == latest["rendered_md"]:
                return
            parent = db.execute(
                "SELECT rendered_md FROM revisions WHERE revision_id = ?",
                (latest["parent_revision_id"],),
            ).fetchone()
            if actual is None and latest["parent_revision_id"] is None:
                self._replace(path, latest["rendered_md"])
            elif parent is not None and actual == parent["rendered_md"]:
                self._replace(path, latest["rendered_md"])
            else:
                raise HistoryConflict(f"文件 {relative} 与 History 最新修订不一致；已保留磁盘内容")

    @staticmethod
    def _put_evidence(db: sqlite3.Connection, revision_id: str, fields: dict) -> None:
        run_id = str(fields.get("run_id") or "")
        for kind in (
            "user_input", "main_delegation", "reviewer_thinking", "reviewer_output",
            "reviewer_delegation", "writer_thinking", "polisher_thinking",
        ):
            content = str(fields.get(kind) or "")
            if not content:
                continue
            db.execute("""
                INSERT INTO evidence(evidence_id, revision_id, kind, content, run_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(revision_id, kind, run_id)
                DO UPDATE SET content = excluded.content
            """, (uuid.uuid4().hex, revision_id, kind, content, run_id))

    def record_revision(self, path: Path, revision_id: str, parent_revision_id: str | None,
                        body: str, rendered: str, *, previous_body: str = "",
                        previous_rendered: str = "", actor: str = "", operation_id: str = "",
                        evidence: dict | None = None, dependencies: dict[str, str] | None = None) -> None:
        relative = self._relative(path)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            document = db.execute("SELECT * FROM documents WHERE path = ?", (relative,)).fetchone()
            if document is None:
                document_id = uuid.uuid4().hex
                db.execute("INSERT INTO documents(document_id, path) VALUES (?, ?)", (document_id, relative))
                latest = None
            else:
                document_id = document["document_id"]
                latest = document["latest_revision_id"]
            if latest is None and parent_revision_id:
                # An existing project starts with a verifiable current-file baseline.
                db.execute("""
                    INSERT INTO revisions(revision_id, document_id, version_no,
                                          parent_revision_id, body_md, rendered_md, body_sha256, actor)
                    VALUES (?, ?, 1, NULL, ?, ?, ?, 'baseline')
                """, (parent_revision_id, document_id, previous_body, previous_rendered,
                      self._digest(previous_body)))
                latest = parent_revision_id
            if latest != parent_revision_id:
                raise HistoryConflict(f"{relative} 的 History 父修订不匹配")
            row = db.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM revisions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            db.execute("""
                INSERT INTO revisions(revision_id, document_id, version_no, parent_revision_id,
                                      body_md, rendered_md, body_sha256, actor, operation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (revision_id, document_id, row[0], parent_revision_id, body, rendered,
                  self._digest(body), actor, operation_id))
            for dep_path, dep_revision in (dependencies or {}).items():
                if dep_revision and dep_path.replace("\\", "/") != relative:
                    db.execute("""
                        INSERT INTO dependencies(revision_id, path, dependency_revision_id)
                        VALUES (?, ?, ?)
                    """, (revision_id, dep_path.replace("\\", "/"), str(dep_revision)))
            self._put_evidence(db, revision_id, evidence or {})
            db.execute("UPDATE documents SET latest_revision_id = ? WHERE document_id = ?",
                       (revision_id, document_id))

    def add_evidence(self, revision_id: str, fields: dict) -> None:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone() is None:
                raise KeyError(revision_id)
            self._put_evidence(db, revision_id, fields)

    def ensure_baseline(self, path: Path) -> str:
        """Import only the verifiable current file when no earlier History exists."""
        from novelagent.versioning import revision_manager

        self.recover_file(path)
        relative = self._relative(path)
        rendered = path.read_text(encoding="utf-8")
        info = revision_manager.parse(rendered)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT latest_revision_id FROM documents WHERE path = ?", (relative,)).fetchone()
            if current:
                if current[0] != info.revision_id:
                    raise HistoryConflict(f"{relative} 与 History 最新修订不一致")
                return info.revision_id
            document_id = uuid.uuid4().hex
            db.execute("INSERT INTO documents VALUES (?, ?, ?)", (document_id, relative, info.revision_id))
            db.execute("""
                INSERT INTO revisions(revision_id, document_id, version_no, parent_revision_id,
                                      body_md, rendered_md, body_sha256, actor)
                VALUES (?, ?, 1, NULL, ?, ?, ?, 'baseline')
            """, (info.revision_id, document_id, info.body, rendered, self._digest(info.body)))
        return info.revision_id

    def record_no_change(self, revision_id: str, opinion: str, rationale: str) -> None:
        if not opinion or not rationale:
            raise ValueError("无需修改必须包含意见和明确依据")
        with self._connect() as db:
            if db.execute("SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone() is None:
                raise KeyError(revision_id)
            db.execute("INSERT INTO no_change VALUES (?, ?, ?, ?, datetime('now'))",
                       (uuid.uuid4().hex, revision_id, opinion, rationale))

    def get_revision(self, revision_id: str) -> dict | None:
        if not self.db_path.exists():
            return None
        with self._connect() as db:
            row = db.execute("""
                SELECT r.*, d.path, d.document_id FROM revisions r
                JOIN documents d ON d.document_id = r.document_id
                WHERE r.revision_id = ?
            """, (revision_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["dependencies"] = {
                dep["path"]: dep["dependency_revision_id"] for dep in db.execute(
                    "SELECT path, dependency_revision_id FROM dependencies WHERE revision_id = ?", (revision_id,),
                )
            }
            result["evidence"] = [dict(item) for item in db.execute(
                "SELECT kind, content, run_id FROM evidence WHERE revision_id = ? ORDER BY created_at, rowid",
                (revision_id,),
            )]
            result["no_change"] = [dict(item) for item in db.execute(
                "SELECT opinion, rationale, created_at FROM no_change WHERE revision_id = ? ORDER BY created_at, rowid",
                (revision_id,),
            )]
            return result

    def latest(self, path: str) -> dict | None:
        if not self.db_path.exists():
            return None
        with self._connect() as db:
            row = db.execute("SELECT latest_revision_id FROM documents WHERE path = ?",
                             (path.replace("\\", "/"),)).fetchone()
        return self.get_revision(row[0]) if row and row[0] else None

    def get_version(self, path: str, version_no: int) -> dict | None:
        if not self.db_path.exists():
            return None
        with self._connect() as db:
            row = db.execute("""
                SELECT r.revision_id FROM revisions r
                JOIN documents d ON d.document_id = r.document_id
                WHERE d.path = ? AND r.version_no = ?
            """, (path.replace("\\", "/"), version_no)).fetchone()
        return self.get_revision(row[0]) if row else None

    def list_versions(self, path: str) -> list[dict]:
        if not self.db_path.exists():
            return []
        with self._connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT r.revision_id, r.version_no, r.parent_revision_id, r.created_at,
                       r.actor, r.operation_id, r.body_sha256
                FROM revisions r JOIN documents d ON d.document_id = r.document_id
                WHERE d.path = ? ORDER BY r.version_no
            """, (path.replace("\\", "/"),))]

    def related(self, *, paths: list[str] | None = None, revision_ids: list[str] | None = None,
                quoted_text: str = "", limit: int = 8) -> list[dict]:
        """Find candidate versions; consumers may inspect full evidence on demand."""
        path_set = {path.replace("\\", "/") for path in paths or []}
        revision_set = set(revision_ids or [])
        if not self.db_path.exists():
            return []
        with self._connect() as db:
            if path_set or revision_set:
                clauses = []
                args = []
                if path_set:
                    clauses.append(f"d.path IN ({','.join('?' for _ in path_set)})")
                    args.extend(path_set)
                if revision_set:
                    clauses.append(f"r.revision_id IN ({','.join('?' for _ in revision_set)})")
                    args.extend(revision_set)
                rows = db.execute(f"""
                    SELECT r.rowid AS history_rowid, r.revision_id, r.parent_revision_id, r.version_no, r.body_md,
                           r.created_at, d.path
                    FROM revisions r JOIN documents d ON d.document_id = r.document_id
                    WHERE {' OR '.join(clauses)} ORDER BY r.rowid DESC LIMIT 100
                """, args).fetchall()
            elif quoted_text and len(quoted_text.strip()) >= 12:
                rows = db.execute("""
                    SELECT r.rowid AS history_rowid, r.revision_id, r.parent_revision_id,
                           r.version_no, r.body_md, r.created_at, d.path
                    FROM revisions r JOIN documents d ON d.document_id = r.document_id
                    WHERE instr(r.body_md, ?) > 0
                    ORDER BY r.rowid DESC LIMIT 100
                """, (quoted_text.strip(),)).fetchall()
            else:
                rows = db.execute("""
                    SELECT r.rowid AS history_rowid, r.revision_id, r.parent_revision_id, r.version_no, r.body_md,
                           r.created_at, d.path
                    FROM revisions r JOIN documents d ON d.document_id = r.document_id
                    ORDER BY r.rowid DESC LIMIT 500
                """).fetchall()
        ranked = []
        for row in rows:
            score = 100 if row["revision_id"] in revision_set else 0
            if row["path"] in path_set:
                score = max(score, 80)
            if quoted_text and len(quoted_text.strip()) >= 12 and quoted_text.strip() in row["body_md"]:
                score = max(score, 95)
            if score:
                ranked.append((score, row["history_rowid"], row["revision_id"]))
        ranked.sort(reverse=True)
        return [self.get_revision(revision_id) for _, _, revision_id in ranked[:limit]]

    def related_to_events(self, events: list[dict], limit: int = 6) -> list[dict]:
        paths, revisions, quotes = set(), set(), []
        for event in events:
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            for source in (payload, payload.get("params"), payload.get("artifact")):
                if not isinstance(source, dict):
                    continue
                path = source.get("path") or source.get("artifact_path")
                if path and str(path).endswith(".md"):
                    paths.add(str(path).replace("\\", "/"))
                for key in ("revision_id", "artifact_revision_id"):
                    if source.get(key):
                        revisions.add(str(source[key]))
            for revision in payload.get("revision_events") or []:
                if isinstance(revision, dict):
                    if revision.get("path"):
                        paths.add(str(revision["path"]).replace("\\", "/"))
                    if revision.get("revision_id"):
                        revisions.add(str(revision["revision_id"]))
            if event.get("event_type") in {"user_message", "user_answer"}:
                content = str(payload.get("content") or "")
                quotes.extend(part.strip() for match in re.findall(
                    r"[“‘\"]([^”’\"]{12,})[”’\"]|```(?:[^\n]*\n)?([\s\S]{12,}?)```",
                    content,
                ) for part in match if part.strip())
                if len(content) >= 12:
                    quotes.append(content.strip())
        found = self.related(paths=list(paths), revision_ids=list(revisions), limit=limit)
        if not found:
            for quote in quotes:
                found = self.related(quoted_text=quote, limit=limit)
                if found:
                    break
        return found

    @staticmethod
    def prompt_view(revisions: list[dict]) -> list[dict]:
        return [{
            "history_revision_id": item["revision_id"], "path": item["path"],
            "version_no": item["version_no"], "parent_revision_id": item["parent_revision_id"],
            "body_excerpt": item["body_md"][:5000], "dependencies": item["dependencies"],
            "evidence": [{"kind": row["kind"], "content": row["content"][:4000]}
                         for row in item["evidence"]],
            "no_change": item["no_change"],
        } for item in revisions]
