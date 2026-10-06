"""Append missing Trace evidence to existing History IDs, without model calls."""

import json
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from novelagent.history.backfill import _requests, _safe_path
from novelagent.history.order_repair import _key, _source_evidence
from novelagent.history.process_repair import _content_key
from novelagent.history.store import DocumentHistoryStore, is_process_event


def _evidence_key(kind, content):
    content = _content_key(kind, content)
    if kind == "file_change_attempt":
        data = json.loads(content)
        content = json.dumps({key: data.get(key) for key in ("tool", "params", "success", "result")}, ensure_ascii=False)
    return kind, _key(content)


def repair_history_evidence(trace_db, project, *, apply=False):
    project = Path(project).resolve()
    history_path = project / ".history/history.sqlite3"
    report = {"project_id": project.name, "mode": "apply" if apply else "dry_run",
              "events_added": 0, "source_links_added": 0, "histories": [], "backup": ""}
    with closing(sqlite3.connect(history_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        units = [dict(r) for r in db.execute("SELECT * FROM history_units")]
        events = [dict(r) for r in db.execute("SELECT * FROM history_events")]
        links = set(tuple(r) for r in db.execute("SELECT source_event_id,event_id FROM history_source_events"))
        orders = {r["event_id"]: r["event_order"] for r in db.execute("SELECT event_id,event_order FROM history_event_order")}
    with closing(sqlite3.connect(Path(trace_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        requests = _requests(db, project.name)
    additions, aliases, refs = [], set(), set()
    store = DocumentHistoryStore(project)
    owners = {}
    with closing(sqlite3.connect(history_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        owners = {u["request_id"]: store._process_owner(db, u["request_id"]) for u in units}
    for request in requests:
        if request["status"] not in {"completed", "failed", "interrupted"}:
            continue
        candidates = _source_evidence(request)
        for unit in units:
            if unit["status"] == "collecting":
                continue
            if unit["request_id"] == request["request_id"]:
                if unit["kind"] not in {"conversation", "requirement", "review", "request"}:
                    continue
                run = "main"
            elif unit["request_id"].startswith(request["request_id"] + ":run:"):
                if unit["kind"] != "document":
                    continue
                run = unit["request_id"].split(":run:", 1)[1]
            else:
                continue
            consumed = Counter()
            for candidate in candidates:
                if candidate["run"] != run or candidate["kind"] in {"reviewer_delegation", "subagent_task", "revision", "revision_reference"}:
                    continue
                if unit["kind"] == "document" and candidate["kind"] == "file_change_attempt":
                    if json.loads(candidate["content"])["params"].get("path") != unit["path"]:
                        continue
                key = _evidence_key(candidate["kind"], candidate["content"])
                owner = owners[unit["request_id"]] if is_process_event(candidate["kind"]) else unit["history_id"]
                matching = [e for e in events if e["history_id"] == owner
                            and _evidence_key(e["kind"], e["content"]) == key
                            and (not is_process_event(candidate["kind"]) or
                                 (json.loads(e["scope_json"]).get("run_id") or "main") == run
                                 or orders.get(e["event_id"]) == candidate["order"]
                                 or any((sid, e["event_id"]) in links for sid in candidate["sources"]))]
                occurrence = consumed[key]
                consumed[key] += 1
                if occurrence < len(matching):
                    if owner != unit["history_id"]:
                        refs.add((unit["history_id"], matching[occurrence]["event_id"]))
                    continue
                eid = uuid.uuid5(uuid.NAMESPACE_URL, "history-evidence:" + owner
                                + ":" + candidate["kind"] + ":" + str(candidate["order"])
                                + ":" + _key(candidate["content"])).hex
                paths = sorted({_safe_path(project, path) for path in
                    re.findall(r"(?:chapters|outlines|world|characters)/[^\s`，。()]+\.md", candidate["content"])} - {""})
                scope = {"source_event_ids": candidate["sources"], "evidence_scope": "run",
                         "run_id": run, "artifact_paths": paths, "provenance": "trace_evidence_repair"}
                if owner != unit["history_id"]:
                    refs.add((unit["history_id"], eid))
                additions.append((eid, owner, candidate["kind"], candidate["content"], "",
                                  candidate["occurred_at"] or request["started_at"], json.dumps(scope, ensure_ascii=False)))
                aliases.update((sid, eid) for sid in candidate["sources"])
                aliases.update((sid, eid) for part in candidate["parts"] for sid in part["sources"])
    additions = list({row[0]: row for row in additions}.values())
    report["events_added"] = len(additions)
    report["source_links_added"] = len(aliases - links)
    report["histories"] = sorted({row[1] for row in additions})
    with closing(sqlite3.connect(history_path.as_uri() + "?mode=ro", uri=True)) as db:
        refs -= set(tuple(r) for r in db.execute("SELECT history_id,event_id FROM history_requirement_refs"))
    report["references_added"] = len(refs)
    if not apply or not (additions or refs):
        return report
    backup = project / ".history/migration_backups" / datetime.now(timezone.utc).strftime("evidence-repair-%Y%m%dT%H%M%S.%fZ.sqlite3")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(history_path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    report["backup"] = str(backup)
    with closing(sqlite3.connect(history_path)) as db:
        with db:
            db.executemany("""INSERT INTO history_events
                (event_id,history_id,kind,content,revision_id,created_at,scope_json) VALUES (?,?,?,?,?,?,?)""", additions)
            db.executemany("INSERT OR IGNORE INTO history_source_events VALUES (?,?)", sorted(aliases))
            db.executemany("INSERT OR IGNORE INTO history_requirement_refs VALUES (?,?)", sorted(refs))
    # Keep analysis status unchanged: offline repair must not enqueue paid extraction.
    return report
