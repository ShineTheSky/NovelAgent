"""Move shared run evidence to demand/review History, retaining old IDs as aliases."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from novelagent.history.store import DocumentHistoryStore, is_process_event


def _content_key(kind, content):
    if kind == "document_reference":
        try:
            data = json.loads(content)
            # Old imports sometimes included redundant revision/path metadata.
            return json.dumps({"params": data.get("params") or {"path": data.get("path")},
                               "content": data.get("content")}, ensure_ascii=False, sort_keys=True)
        except (ValueError, AttributeError):
            pass
    return content


def repair_process_ownership(project, *, apply=False):
    store = DocumentHistoryStore(Path(project))
    report = {"mode": "apply" if apply else "dry_run", "moved": 0, "aliases": 0,
              "references": 0, "redirected_links": 0, "backup": ""}
    with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        events = [dict(r) for r in db.execute("SELECT *,rowid AS position FROM history_events ORDER BY rowid")]
        units = {r["history_id"]: dict(r) for r in db.execute("SELECT * FROM history_units")}
        orders = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM history_event_order")}
        sources = defaultdict(set)
        for sid, eid in db.execute("SELECT source_event_id,event_id FROM history_source_events"):
            sources[eid].add(sid)
        groups = defaultdict(list)
        for event in events:
            if not is_process_event(event["kind"]) or not event["content"]:
                continue
            order = orders.get(event["event_id"], {})
            if order.get("order_status") == "duplicate":
                continue
            unit = units[event["history_id"]]
            scope = json.loads(event["scope_json"])
            request = scope.get("process_request_id") or unit["request_id"]
            owner = store._process_owner(db, request)
            run = scope.get("run_id") or (request.split(":run:", 1)[1] if ":run:" in request else "main")
            origins = sources[event["event_id"]] | set(scope.get("source_event_ids", []))
            for marker in (":part:evt_", ":source:evt_"):
                if marker in event["event_id"]:
                    origins.add("evt_" + event["event_id"].split(marker, 1)[1])
            if order.get("order_status") == "superseded":
                for part_id, part_order in orders.items():
                    if part_order.get("original_event_id") == event["event_id"]:
                        origins.update(sources[part_id])
                        if ":part:evt_" in part_id:
                            origins.add("evt_" + part_id.split(":part:evt_", 1)[1])
            # Exact source identity distinguishes genuinely repeated calls.
            # Unidentified evidence is moved, but never collapsed by text alone.
            bucket = groups[(owner, run, event["kind"], _content_key(event["kind"], event["content"]))]
            matches = [group for group in bucket if origins and origins & group[0]]
            if matches:
                target = matches[0]
                target[0].update(origins)
                target[1].append(event)
                for other in matches[1:]:
                    target[0].update(other[0])
                    target[1].extend(other[1])
                    bucket.remove(other)
            else:
                bucket.append([set(origins), [event]])
        changes, refs = [], set()
        grouped = [(key, group[1]) for key, bucket in groups.items() for group in bucket]
        for (owner, run, kind, content), copies in grouped:
            canonical = min(copies, key=lambda e: (
                orders.get(e["event_id"], {}).get("order_status") == "superseded",
                e["history_id"] != owner, e["position"]))
            eid = canonical["event_id"]
            for event in copies:
                unit = units[event["history_id"]]
                scope = json.loads(event["scope_json"])
                request = scope.get("process_request_id") or unit["request_id"]
                related = [u["history_id"] for u in units.values() if u["request_id"] == request]
                refs.update((hid, eid) for hid in [event["history_id"], *related] if hid != owner)
                if event is canonical:
                    if event["history_id"] != owner:
                        report["moved"] += 1
                        changes.append(("move", event, owner, eid, {**scope, "run_id": run,
                            "process_request_id": request, "evidence_scope": "run"}))
                else:
                    report["aliases"] += 1
                    changes.append(("alias", event, owner, eid, scope))
        existing = set(tuple(r) for r in db.execute("SELECT history_id,event_id FROM history_requirement_refs"))
        refs -= existing
        report["references"] = len(refs)
        redirects = {eid: row["canonical_event_id"] for eid, row in orders.items()
                     if row.get("canonical_event_id") and row.get("order_status") == "duplicate"}
        report["redirected_links"] = sum(1 for eid in redirects if sources[eid] or any(e == eid for _, e in existing))
    if not apply or not (changes or refs or report["redirected_links"]):
        return report
    backup = store.db_path.parent / "migration_backups" / datetime.now(timezone.utc).strftime("process-ownership-%Y%m%dT%H%M%S.%fZ.sqlite3")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store.db_path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    report["backup"] = str(backup)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for action, event, owner, eid, scope in changes:
            if action == "move":
                db.execute("UPDATE history_events SET history_id=?,scope_json=? WHERE event_id=?",
                           (owner, json.dumps(scope, ensure_ascii=False), eid))
            else:
                # Preserve source/event IDs used by existing Memory; aliases have
                # no second body and are excluded from extraction by order status.
                db.execute("UPDATE history_events SET content='' WHERE event_id=?", (event["event_id"],))
                db.execute("""INSERT INTO history_event_order(event_id,order_status,canonical_event_id)
                    VALUES (?,'duplicate',?) ON CONFLICT(event_id) DO UPDATE SET
                    order_status='duplicate',canonical_event_id=excluded.canonical_event_id""", (event["event_id"], eid))
                redirects[event["event_id"]] = eid
        for alias, canonical in redirects.items():
            seen = {alias}
            while canonical in redirects:
                if canonical in seen:
                    raise ValueError("Cyclic History process alias")
                seen.add(canonical)
                canonical = redirects[canonical]
            db.execute("UPDATE history_event_order SET canonical_event_id=? WHERE event_id=?", (canonical, alias))
            db.execute("INSERT OR IGNORE INTO history_source_events SELECT source_event_id,? FROM history_source_events WHERE event_id=?", (canonical, alias))
            db.execute("DELETE FROM history_source_events WHERE event_id=?", (alias,))
            db.execute("INSERT OR IGNORE INTO history_requirement_refs SELECT history_id,? FROM history_requirement_refs WHERE event_id=?", (canonical, alias))
            db.execute("DELETE FROM history_requirement_refs WHERE event_id=?", (alias,))
        db.executemany("INSERT OR IGNORE INTO history_requirement_refs VALUES (?,?)", sorted(refs))
    # Offline storage repair does not enqueue extraction or change analysis status.
    return report
