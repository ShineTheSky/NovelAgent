"""Apply explicitly reviewed legacy provenance corrections without calling an LLM."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3

import yaml

from novelagent.history.store import DocumentHistoryStore
from novelagent.history.sources import normalize_history_roots


def _read_db(path):
    db = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def repair_sources(trace_db: Path, project: Path, plan: dict, *, apply=False) -> dict:
    project = Path(project).resolve()
    if project.name != plan["project_id"]:
        raise ValueError("Repair plan belongs to another project")
    store = DocumentHistoryStore(project)
    with closing(_read_db(store.db_path)) as db:
        units = {r["history_id"]: dict(r) for r in db.execute("SELECT * FROM history_units")}
        events = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM history_events")}
    source_ids = {n["source_event_id"] for n in plan.get("review_nodes", [])}
    source_ids.update(e["source_event_id"] for item in plan["records"] for e in item.get("add_evidence", []))
    with closing(_read_db(trace_db)) as db:
        sources = {r["event_id"]: dict(r) for r in db.execute("""SELECT e.*, t.project_id
            FROM trace_events e JOIN traces t ON t.trace_id = e.trace_id
            WHERE t.project_id = ?""", (project.name,)) if r["event_id"] in source_ids}
    if source_ids - sources.keys():
        raise ValueError(f"Missing project Trace evidence: {sorted(source_ids - sources.keys())}")

    nodes = {}
    for spec in plan.get("review_nodes", []):
        source = sources[spec["source_event_id"]]
        payload = json.loads(source["payload_json"])
        parent = units[spec["requirement_history_id"]]
        if parent["kind"] != "requirement" or source["trace_id"] not in json.loads(parent["trace_ids_json"]):
            raise ValueError("Review source does not belong to its requirement")
        if source["event_type"] != "assistant_turn" or spec["evidence_origin"] != "review_retelling":
            raise ValueError("This repair imports explicit main-Agent retellings only")
        content = str(payload.get("content") or "")
        if spec["anchor"] not in content:
            raise ValueError("Review anchor missing from original Trace")
        path = str(spec.get("path") or "").replace("\\", "/")
        if path and (not (project / path).resolve().is_relative_to(project) or path.startswith(".") or not path.endswith(".md")):
            raise ValueError("Invalid review path")
        request_id = f"{parent['request_id']}:review-evidence:{source['event_id']}"
        hid = store._unit_id(request_id, "request")
        eid = f"{hid}:source:{source['event_id']}"
        nodes[source["event_id"]] = {"history_id": hid, "request_id": request_id,
            "event_id": eid, "parent": parent, "path": path, "source": source, "content": content}
        events[eid] = {"event_id": eid, "history_id": hid, "kind": "review_retelling", "content": content, "revision_id": ""}

    records = {}
    for layer in ("insight", "memory", "pattern"):
        for path in (project / f".{layer}").glob("*/*.md"):
            try:
                _, header, body = path.read_text(encoding="utf-8").split("---", 2)
                record = yaml.safe_load(header)
            except (ValueError, yaml.YAMLError):
                continue
            if isinstance(record, dict) and record.get("id"):
                records[record["id"]] = (path, record, body)
    updates = []
    aliases = []
    for spec in plan["records"]:
        path, record, body = records[spec["id"]]
        if record.get("title") != spec["expected_title"]:
            raise ValueError(f"Record changed since review: {spec['id']}")
        revised = {**record}
        links = [dict(link) for link in record.get("history_source_links", [])]
        decisions = dict(record.get("history_source_decisions") or {})
        related = list(record.get("related_history_ids") or [])
        review_source = spec.get("review_source_event_id")
        if review_source:
            node = nodes[review_source]
            if spec["evidence_anchor"] not in node["content"]:
                raise ValueError("Claim anchor missing from review retelling")
            context_ids = set(spec.get("context_source_event_ids", []))
            for link in links:
                if link.get("source_event_id") in context_ids:
                    link.update(match="context", reason="不同问题的上下文，不直接支撑本条机制记忆")
                    decisions[link["source_event_id"]] = {"match": "context"}
                    owner = units[link["history_id"]]
                    related.append(owner.get("requirement_history_id") or owner["history_id"])
            links = [link for link in links if link.get("source_event_id") != review_source]
            links.append({"source_event_id": review_source, "history_id": node["history_id"],
                          "history_event_ids": [node["event_id"]], "match": "evidence", "evidence_origin": "review_retelling"})
            decisions[review_source] = {"match": "evidence", "history_id": node["history_id"]}
            revised["history_ids"] = [node["history_id"]]
            related.append(node["parent"]["history_id"])
        for extra in spec.get("add_evidence", []):
            event = events[extra["history_event_id"]]
            source = sources[extra["source_event_id"]]
            payload = json.loads(source["payload_json"])
            source_text = ((payload.get("params") or {}).get("task") if event["kind"] == "main_delegation"
                           else payload.get("result") or payload.get("data"))
            owner = units[event["history_id"]]
            root = units.get(owner.get("requirement_history_id")) or owner
            if source_text != event["content"] or source["trace_id"] not in json.loads(root["trace_ids_json"]):
                raise ValueError("Additional History evidence does not match its original Trace")
            if extra["anchor"] not in event["content"]:
                raise ValueError("Additional evidence anchor missing")
            link = {"source_event_id": source["event_id"], "history_id": event["history_id"],
                    "history_event_ids": [event["event_id"]], "match": "evidence"}
            if link not in links:
                links.append(link)
            decisions[source["event_id"]] = {"match": "evidence", "history_id": event["history_id"]}
            aliases.append((source["event_id"], event["event_id"]))
        revised["history_source_links"] = links
        revised["history_source_decisions"] = decisions
        revised["related_history_ids"] = list(dict.fromkeys(related))
        revised["history_event_ids"] = list(dict.fromkeys(e for link in links if link["match"] == "evidence" for e in link["history_event_ids"]))
        semantic = []
        extra_kinds = {e["history_event_id"]: e["source_kind"] for e in spec.get("add_evidence", [])}
        for link in links:
            if link["match"] != "evidence":
                continue
            for eid in link["history_event_ids"]:
                event = events[eid]
                source_kind = extra_kinds.get(eid) or ("user" if event["kind"] in {"user_input", "user_answer"} else "agent")
                retelling = event["kind"] == "review_retelling"
                content = event["content"]
                anchor = spec.get("evidence_anchor") if retelling else None
                start = max(0, content.find(anchor) - 150) if anchor else 0
                semantic.append({"claim": record.get("claim") or record["title"], "history_id": link["history_id"],
                    "history_event_ids": [eid], "source_kind": "main_agent" if retelling else source_kind,
                    "evidence_origin": "review_retelling" if retelling else event["kind"],
                    "evidence_status": "retelling_only" if retelling else "verified_event",
                    "scope": spec.get("artifact_path") or record.get("artifact_path") or "project",
                    "why": "", "attributes": {"original_report_available": False} if retelling else {},
                    "history_excerpts": [{"event_id": eid, "kind": event["kind"], "content": content[start:start + 700]}]})
        # Preserve any richer evidence already extracted; this migration only appends literal provenance.
        existing = list(record.get("semantic_evidence") or [])
        revised["semantic_evidence"] = existing + [row for row in semantic if row not in existing]
        revised["provenance_notes"] = list(dict.fromkeys([*(record.get("provenance_notes") or []), spec["note"]]))
        if spec.get("body_source_note") and spec["body_source_note"] not in body:
            body = body.replace("## 用户要求\n", "## 用户要求与主 Agent 执行约束\n" + spec["body_source_note"] + "\n\n", 1)
        revised.update(normalize_history_roots(project, revised))
        changed = revised != record or body != records[spec["id"]][2]
        updates.append((path, revised, body, changed))

    report = {"mode": "apply" if apply else "dry_run", "project_id": project.name,
        "review_nodes": [{"history_id": n["history_id"], "source_event_id": sid, "evidence_origin": "review_retelling",
                          "new": n["history_id"] not in units} for sid, n in nodes.items()],
        "changed_records": [r["id"] for _, r, _, changed in updates if changed], "backup": None}
    if not apply or (not report["changed_records"] and not any(n["new"] for n in report["review_nodes"])):
        return report
    backup = project / ".history/migration_backups" / datetime.now(timezone.utc).strftime("source-repair-%Y%m%dT%H%M%S.%fZ")
    backup.mkdir(parents=True)
    with closing(_read_db(store.db_path)) as db, closing(sqlite3.connect(backup / "history.sqlite3")) as copy:
        db.backup(copy)
    for path, _, _, changed in updates:
        if changed:
            target = backup / path.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    store._replace(backup / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
    with store._connect() as db:
        for node in nodes.values():
            parent, source = node["parent"], node["source"]
            db.execute("""INSERT OR IGNORE INTO history_units
                (history_id,request_id,parent_request_id,session_id,session_turn_no,kind,path,status,
                 analysis_status,assistant_response,trace_ids_json,created_at,requirement_history_id)
                VALUES (?,?,?,?,?,'review',?,'completed','complete',?,?,?,?)""",
                (node["history_id"], node["request_id"], parent["request_id"], parent["session_id"], parent["session_turn_no"],
                 node["path"], node["content"], json.dumps([source["trace_id"]]), source["created_at"], parent["history_id"]))
            scope = {"evidence_origin": "review_retelling", "original_report_available": False,
                     "source_event_id": source["event_id"], "source_trace_id": source["trace_id"]}
            db.execute("""INSERT OR IGNORE INTO history_events (event_id,history_id,kind,content,created_at,scope_json)
                VALUES (?,?,'review_retelling',?,?,?)""", (node["event_id"], node["history_id"], node["content"], source["created_at"], json.dumps(scope)))
            db.execute("INSERT OR IGNORE INTO history_source_events VALUES (?,?)", (source["event_id"], node["event_id"]))
            db.execute("INSERT OR IGNORE INTO history_event_order(event_id,event_order,occurred_at,order_status,original_event_id) VALUES (?,?,?,'verified',?)",
                       (node["event_id"], source["sequence_no"] * 1000, source["created_at"], source["event_id"]))
        db.executemany("INSERT OR IGNORE INTO history_source_events VALUES (?,?)", aliases)
    for path, record, body, changed in updates:
        if changed:
            store._replace(path, "---\n" + yaml.safe_dump(record, allow_unicode=True, sort_keys=False).rstrip() + "\n---" + body)
    report["backup"] = str(backup)
    store._replace(backup / "result.json", json.dumps(report, ensure_ascii=False, indent=2))
    return report
