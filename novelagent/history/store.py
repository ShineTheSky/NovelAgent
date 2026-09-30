"""Complete, project-local revisions and their original evidence.

The database is authoritative.  The Markdown file is a materialized current
revision and can be repaired after a crash between the database commit and
the atomic file replacement.
"""

from __future__ import annotations

import hashlib
import json
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
            CREATE TABLE IF NOT EXISTS history_units (
                history_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                parent_request_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL,
                session_turn_no INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                base_revision_id TEXT NOT NULL DEFAULT '',
                final_revision_id TEXT NOT NULL DEFAULT '',
                previous_history_id TEXT NOT NULL DEFAULT '',
                previous_attempt_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'collecting',
                analysis_status TEXT NOT NULL DEFAULT 'pending',
                assistant_response TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '[]',
                selected_turns_json TEXT NOT NULL DEFAULT '[]',
                trace_ids_json TEXT NOT NULL DEFAULT '[]',
                prepared_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(request_id, kind, path)
            );
            CREATE TABLE IF NOT EXISTS history_events (
                event_id TEXT PRIMARY KEY,
                history_id TEXT NOT NULL REFERENCES history_units(history_id),
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                revision_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_history_events_unit
                ON history_events(history_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_history_units_path
                ON history_units(path, created_at);
            CREATE INDEX IF NOT EXISTS idx_history_units_status
                ON history_units(status, analysis_status);
        """)
        if "parent_request_id" not in {row[1] for row in db.execute("PRAGMA table_info(history_units)")}:
            db.execute("ALTER TABLE history_units ADD COLUMN parent_request_id TEXT NOT NULL DEFAULT ''")
        if "previous_history_id" not in {row[1] for row in db.execute("PRAGMA table_info(history_units)")}:
            db.execute("ALTER TABLE history_units ADD COLUMN previous_history_id TEXT NOT NULL DEFAULT ''")
            for row in db.execute("""SELECT history_id FROM history_units
                WHERE kind = 'document' ORDER BY created_at, rowid""").fetchall():
                self._link_document_history(db, row["history_id"])
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
    def _unit_id(request_id: str, kind: str, path: str = "") -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"history:{request_id}:{kind}:{path}").hex

    def start_request(self, request_id: str, session_id: str, user_input: str,
                      context: list[dict] | None = None, parent_request_id: str = "") -> str:
        history_id = self._unit_id(request_id, "request")
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO history_units
                (history_id, request_id, parent_request_id, session_id, kind, context_json)
                VALUES (?, ?, ?, ?, 'request', ?)""",
                (history_id, request_id, parent_request_id, session_id,
                 json.dumps(context or [], ensure_ascii=False)))
            db.execute("""INSERT OR IGNORE INTO history_events(event_id, history_id, kind, content)
                VALUES (?, ?, 'user_input', ?)""",
                (f"{history_id}:input", history_id, user_input))
        return history_id

    def update_request_context(self, request_id: str, context: list[dict]) -> None:
        with self._connect() as db:
            db.execute("UPDATE history_units SET context_json = ? WHERE history_id = ?",
                       (json.dumps(context, ensure_ascii=False), self._unit_id(request_id, "request")))

    def append_request_event(self, request_id: str, kind: str, content: str) -> str:
        return self._append_event(self._unit_id(request_id, "request"), kind, content)

    @staticmethod
    def run_request_id(request_id: str, run_id: str) -> str:
        return f"{request_id}:run:{run_id}"

    def _append_event(self, history_id: str, kind: str, content: str,
                      revision_id: str = "", event_id: str = "") -> str:
        event_id = event_id or uuid.uuid4().hex
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO history_events
                (event_id, history_id, kind, content, revision_id) VALUES (?, ?, ?, ?, ?)""",
                (event_id, history_id, kind, content, revision_id))
        return event_id

    def _document_unit(self, db: sqlite3.Connection, request_id: str, path: str) -> str:
        path = path.replace("\\", "/")
        request = db.execute("SELECT session_id FROM history_units WHERE history_id = ?",
                             (self._unit_id(request_id, "request"),)).fetchone()
        if request is None:
            raise KeyError(f"History request not found: {request_id}")
        history_id = self._unit_id(request_id, "document", path)
        previous = db.execute("""SELECT history_id, status FROM history_units
            WHERE kind = 'document' AND path = ? AND request_id != ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1""", (path, request_id)).fetchone()
        db.execute("""INSERT OR IGNORE INTO history_units
            (history_id, request_id, session_id, kind, path, previous_attempt_id)
            VALUES (?, ?, ?, 'document', ?, ?)""",
            (history_id, request_id, request["session_id"], path,
             previous["history_id"] if previous and previous["status"] in {"failed", "interrupted"} else ""))
        return history_id

    @staticmethod
    def _link_document_history(db: sqlite3.Connection, history_id: str) -> None:
        current = db.execute("""SELECT history_id, path, base_revision_id, final_revision_id,
            created_at, rowid FROM history_units WHERE history_id = ? AND kind = 'document'""",
            (history_id,)).fetchone()
        if not current:
            return
        previous = None
        if current["base_revision_id"]:
            previous = db.execute("""SELECT history_id FROM history_units
                WHERE kind = 'document' AND path = ? AND history_id != ?
                  AND final_revision_id = ?
                  AND (created_at < ? OR (created_at = ? AND rowid < ?))
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (current["path"], history_id, current["base_revision_id"],
                 current["created_at"], current["created_at"], current["rowid"])).fetchone()
        if not previous and not current["final_revision_id"]:
            previous = db.execute("""SELECT history_id FROM history_units
                WHERE kind = 'document' AND path = ? AND final_revision_id != ''
                  AND (created_at < ? OR (created_at = ? AND rowid < ?))
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (current["path"], current["created_at"], current["created_at"],
                 current["rowid"])).fetchone()
        db.execute("UPDATE history_units SET previous_history_id = ? WHERE history_id = ?",
                   (previous["history_id"] if previous else "", history_id))

    def attach_revision(self, request_id: str, path: str, revision_id: str,
                        base_revision_id: str = "") -> str:
        with self._connect() as db:
            history_id = self._attach_revision(db, request_id, path, revision_id, base_revision_id)
        return history_id

    def _attach_revision(self, db: sqlite3.Connection, request_id: str, path: str,
                         revision_id: str, base_revision_id: str) -> str:
        history_id = self._document_unit(db, request_id, path)
        db.execute("""UPDATE history_units SET
            base_revision_id = CASE WHEN NOT EXISTS (
                SELECT 1 FROM history_events WHERE history_id = ? AND kind = 'revision'
            ) THEN ? ELSE base_revision_id END,
            final_revision_id = ? WHERE history_id = ?""",
            (history_id, base_revision_id, revision_id, history_id))
        db.execute("""INSERT OR IGNORE INTO history_events
            (event_id, history_id, kind, content, revision_id) VALUES (?, ?, 'revision', ?, ?)""",
            (f"{history_id}:revision:{revision_id}", history_id, revision_id, revision_id))
        return history_id

    def append_document_event(self, request_id: str, path: str, kind: str, content: str,
                              revision_id: str = "", event_id: str = "") -> str:
        with self._connect() as db:
            history_id = self._document_unit(db, request_id, path)
            db.execute("""INSERT OR IGNORE INTO history_events
                (event_id, history_id, kind, content, revision_id) VALUES (?, ?, ?, ?, ?)""",
                (event_id or uuid.uuid4().hex, history_id, kind, content, revision_id))
            if kind == "evaluation_no_change" and revision_id:
                db.execute("""UPDATE history_units SET
                    base_revision_id = CASE WHEN base_revision_id = '' THEN ? ELSE base_revision_id END
                    WHERE history_id = ?""", (revision_id, history_id))
        return history_id

    def close_request(self, request_id: str, status: str, answer: str,
                      trace_ids: list[str] | None = None, session_turn_no: int = 0) -> list[str]:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(status)
        request_history_id = self._unit_id(request_id, "request")
        with self._connect() as db:
            request = db.execute("SELECT * FROM history_units WHERE history_id = ?",
                                 (request_history_id,)).fetchone()
            if request is None:
                return []
            document_ids = [row[0] for row in db.execute("""SELECT history_id FROM history_units
                WHERE request_id = ? AND kind = 'document' ORDER BY rowid""", (request_id,))]
            child_ids = [row[0] for row in db.execute("""SELECT d.history_id FROM history_units d
                JOIN history_units r ON r.request_id = d.request_id AND r.kind = 'request'
                WHERE r.parent_request_id = ? AND d.kind = 'document'
                ORDER BY d.rowid""", (request_id,))]
            db.execute("""UPDATE history_units SET status = ?, assistant_response = ?,
                trace_ids_json = ?, session_turn_no = ? WHERE request_id = ?""",
                (status, answer, json.dumps(trace_ids or [request_id]), session_turn_no, request_id))
            if document_ids or child_ids:
                db.execute("UPDATE history_units SET kind = 'request' WHERE history_id = ?",
                           (request_history_id,))
            elif request["parent_request_id"]:
                db.execute("UPDATE history_units SET kind = 'request' WHERE history_id = ?",
                           (request_history_id,))
            else:
                db.execute("UPDATE history_units SET kind = 'conversation' WHERE history_id = ?",
                           (request_history_id,))
            if status != "completed":
                db.execute("UPDATE history_units SET analysis_status = 'deferred' WHERE request_id = ?",
                           (request_id,))
            elif document_ids or child_ids:
                db.execute("UPDATE history_units SET analysis_status = 'routed' WHERE history_id = ?",
                           (request_history_id,))
                db.execute("""UPDATE history_units SET status = 'failed', analysis_status = 'deferred'
                    WHERE request_id = ? AND kind = 'document' AND final_revision_id = ''
                    AND history_id IN (SELECT history_id FROM history_events WHERE kind = 'tool_failure')""",
                    (request_id,))
                db.execute("""UPDATE history_units SET status = 'failed', analysis_status = 'deferred'
                    WHERE request_id = ? AND kind = 'document'
                    AND history_id IN (SELECT history_id FROM history_events WHERE kind = 'workflow_failure')""",
                    (request_id,))
            elif request["parent_request_id"]:
                db.execute("UPDATE history_units SET analysis_status = 'routed' WHERE history_id = ?",
                           (request_history_id,))
            for history_id in document_ids:
                self._link_document_history(db, history_id)
        if status != "completed":
            return []
        return [row for row in (document_ids + child_ids or [request_history_id])
                if (self.get_unit(row) or {}).get("status") == "completed"]

    def list_analyzable(self) -> list[str]:
        if not self.db_path.exists():
            return []
        with self._connect() as db:
            return [row[0] for row in db.execute("""SELECT history_id FROM history_units
                WHERE status = 'completed' AND kind IN ('document', 'conversation')
                  AND analysis_status IN ('pending', 'failed') ORDER BY created_at, rowid""")]

    def interrupt_collecting(self) -> int:
        """A process restart cannot resume a suspended model/tool call, but keeps its evidence."""
        if not self.db_path.exists():
            return 0
        with self._connect() as db:
            cursor = db.execute("""UPDATE history_units
                SET status = 'interrupted', analysis_status = 'deferred'
                WHERE status = 'collecting'""")
            return cursor.rowcount

    def get_unit(self, history_id: str) -> dict | None:
        if not self.db_path.exists():
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM history_units WHERE history_id = ?", (history_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["context"] = json.loads(result.pop("context_json"))
            result["selected_turns"] = json.loads(result.pop("selected_turns_json"))
            result["trace_ids"] = json.loads(result.pop("trace_ids_json"))
            result["prepared"] = json.loads(result.pop("prepared_json"))
            ids = [history_id]
            if result["kind"] == "document":
                ids.insert(0, self._unit_id(result["request_id"], "request"))
            result["events"] = [dict(event) for event in db.execute(f"""SELECT * FROM history_events
                WHERE history_id IN ({','.join('?' for _ in ids)}) ORDER BY rowid""", ids)]
            prior = db.execute("SELECT request_id FROM history_units WHERE history_id = ?",
                               (result["previous_attempt_id"],)).fetchone() if result["previous_attempt_id"] else None
            prior_ids = ([self._unit_id(prior["request_id"], "request"), result["previous_attempt_id"]]
                         if prior else [])
            result["previous_attempt_events"] = [dict(event) for event in db.execute(
                "SELECT * FROM history_events WHERE history_id IN (?, ?) ORDER BY rowid",
                prior_ids,
            )] if prior_ids else []
        if result["kind"] == "document":
            revision_ids = list(dict.fromkeys(event["revision_id"] for event in result["events"]
                                                  if event["revision_id"]))
            result["revisions"] = [self.get_revision(revision_id) for revision_id in revision_ids]
        return result

    def list_document_units(self, path: str) -> list[dict]:
        """Read the ordered attempts/evaluations associated with one document."""
        if not self.db_path.exists():
            return []
        with self._connect() as db:
            ids = [row[0] for row in db.execute("""SELECT history_id FROM history_units
                WHERE kind = 'document' AND path = ? ORDER BY created_at, rowid""",
                (path.replace("\\", "/"),))]
        return [self.get_unit(history_id) for history_id in ids]

    def list_files(self, *, limit: int = 25, offset: int = 0) -> dict:
        """List file-level History groups without loading individual evidence."""
        if not self.db_path.exists():
            return {"items": [], "total": 0}
        with self._connect() as db:
            total = db.execute("""SELECT COUNT(DISTINCT path) FROM history_units
                WHERE kind = 'document'""").fetchone()[0]
            rows = db.execute("""SELECT path, COUNT(*) AS history_count,
                       MAX(created_at) AS updated_at,
                       (SELECT recent.history_id FROM history_units recent
                        WHERE recent.kind = 'document' AND recent.path = h.path
                        ORDER BY recent.created_at DESC, recent.rowid DESC LIMIT 1)
                       AS latest_history_id
                    FROM history_units h WHERE kind = 'document'
                    GROUP BY path ORDER BY updated_at DESC, path LIMIT ? OFFSET ?""",
                (limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total}

    def list_units(self, *, kind: str = "", path: str = "", limit: int = 25,
                   offset: int = 0) -> dict:
        """List lightweight project History summaries without loading revision bodies."""
        if kind not in {"", "document", "conversation"}:
            raise ValueError(kind)
        if path and kind != "document":
            raise ValueError("A file path requires kind=document")
        if not self.db_path.exists():
            return {"items": [], "total": 0}
        where = "kind IN ('document', 'conversation')"
        args: list = []
        if kind:
            where += " AND kind = ?"
            args.append(kind)
        if path:
            where += " AND path = ?"
            args.append(path.replace("\\", "/"))
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM history_units WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"""SELECT h.history_id, h.kind, h.path, h.status,
                       h.analysis_status, h.created_at, h.session_turn_no,
                       h.base_revision_id, h.final_revision_id, h.previous_history_id,
                       (SELECT substr(e.content, 1, 240) FROM history_events e
                        WHERE e.history_id = CASE WHEN h.kind = 'document' THEN
                            (SELECT r.history_id FROM history_units r
                             WHERE r.request_id = h.request_id AND r.kind = 'request')
                            ELSE h.history_id END
                          AND e.kind = 'user_input' LIMIT 1) AS user_input,
                       (SELECT substr(e.content, 1, 240) FROM history_events e
                        JOIN history_units r ON r.history_id = e.history_id
                        WHERE r.request_id = h.request_id AND r.kind = 'request'
                          AND e.kind = 'main_delegation' LIMIT 1) AS task_summary
                    FROM history_units h WHERE {where}
                    ORDER BY h.created_at DESC, h.rowid DESC LIMIT ? OFFSET ?""",
                [*args, limit, offset],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total}

    def set_selected_turns(self, history_id: str, turns: dict) -> None:
        with self._connect() as db:
            db.execute("UPDATE history_units SET selected_turns_json = ? WHERE history_id = ?",
                       (json.dumps(turns, ensure_ascii=False), history_id))

    def set_prepared(self, history_id: str, payload: dict) -> None:
        with self._connect() as db:
            db.execute("UPDATE history_units SET prepared_json = ? WHERE history_id = ?",
                       (json.dumps(payload, ensure_ascii=False), history_id))

    def set_analysis_status(self, history_id: str, status: str, error: str = "") -> None:
        with self._connect() as db:
            db.execute("UPDATE history_units SET analysis_status = ?, last_error = ? WHERE history_id = ?",
                       (status, error, history_id))

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
            request_id = str((evidence or {}).get("history_request_id") or "")
            if request_id:
                self._attach_revision(db, request_id, relative, revision_id, parent_revision_id or "")

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

    def record_no_change(self, revision_id: str, opinion: str, rationale: str, *,
                         request_id: str = "", path: str = "") -> str:
        if not opinion or not rationale:
            raise ValueError("无需修改必须包含意见和明确依据")
        with self._connect() as db:
            if db.execute("SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone() is None:
                raise KeyError(revision_id)
            decision_id = uuid.uuid4().hex
            db.execute("INSERT INTO no_change VALUES (?, ?, ?, ?, datetime('now'))",
                       (decision_id, revision_id, opinion, rationale))
            if request_id:
                if not path:
                    row = db.execute("""SELECT d.path FROM revisions r JOIN documents d
                        ON d.document_id = r.document_id WHERE r.revision_id = ?""",
                        (revision_id,)).fetchone()
                    path = str(row[0]) if row else ""
                history_id = self._document_unit(db, request_id, path)
                db.execute("""INSERT INTO history_events
                    (event_id, history_id, kind, content, revision_id)
                    VALUES (?, ?, 'evaluation_no_change', ?, ?)""",
                    (decision_id, history_id, opinion + "\n\n" + rationale, revision_id))
                db.execute("""UPDATE history_units SET
                    base_revision_id = CASE WHEN base_revision_id = '' THEN ? ELSE base_revision_id END
                    WHERE history_id = ?""", (revision_id, history_id))
        return decision_id

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
