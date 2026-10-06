"""Attach exact old tool/dispatch sources in an isolated migration History only."""

import json
import re
import sqlite3
import uuid
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path


def repair_snapshot_sources(destination, trace_database, project_id, legacy_rows):
    destination = Path(destination).resolve()
    history_path = destination / ".history/history.sqlite3"
    if not (destination / "migration_evidence.sqlite3").exists():
        raise ValueError("History repair requires an isolated migration snapshot")
    report = {"sources_mapped": 0, "events_added": 0, "source_links_added": 0, "mappings": [], "unresolved": []}
    wanted = {eid for row in legacy_rows if row["status"] == "trace_rebuild_required" for eid in row["missing_event_ids"]}
    traces = {source["trace_id"] for row in legacy_rows for source in row.get("trace_evidence", []) if source["event_id"] in wanted}
    if not wanted or not history_path.exists():
        return report
    with closing(sqlite3.connect(Path(trace_database).resolve().as_uri() + "?mode=ro", uri=True)) as source_db:
        source_db.row_factory = sqlite3.Row
        sources = []
        for trace_id in sorted(traces):
            # Do not read agent_request_message/context blobs or unrelated traces.
            sources.extend(dict(row) for row in source_db.execute("""SELECT e.* FROM trace_events e
                JOIN traces t ON t.trace_id=e.trace_id WHERE t.project_id=? AND e.trace_id=?
                AND ((e.event_type LIKE '%tool_call' OR e.event_type LIKE '%tool_result')
                     AND json_extract(e.payload_json,'$.tool') IN ('Write','Edit','SubAgent')
                     OR e.event_type LIKE '%subagent_start') ORDER BY e.sequence_no""", (project_id, trace_id)))
    by_id = {row["event_id"]: row for row in sources}
    starts = defaultdict(list)
    for row in sources:
        row["payload"] = json.loads(row["payload_json"])
        if row["event_type"].endswith("subagent_start"):
            starts[(row["trace_id"], row.get("parent_event_id"))].append(row)
    with closing(sqlite3.connect(history_path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        units = [dict(row) for row in db.execute("SELECT * FROM history_units")]
        events = [dict(row) for row in db.execute("SELECT * FROM history_events")]
        queue = defaultdict(deque)
        mapped = set()

        def owned(unit, trace_id, run):
            return trace_id in json.loads(unit.get("trace_ids_json") or "[]") and (
                unit["request_id"].endswith(":run:" + run) if run else unit["request_id"] == trace_id)

        def attach(event, source_ids, origin):
            for eid in source_ids:
                cursor = db.execute("INSERT OR IGNORE INTO history_source_events VALUES (?,?)", (eid, event["event_id"]))
                report["source_links_added"] += cursor.rowcount
                if eid in wanted:
                    mapped.add(eid)
                    report["mappings"].append({"source_event_id": eid, "history_id": event["history_id"],
                        "history_event_id": event["event_id"], "kind": event["kind"], "sequence_no": origin["sequence_no"]})
            # An existing verified canonical occurrence wins over a later identical return alias.
            db.execute("""INSERT INTO history_event_order
                (event_id,event_order,occurred_at,order_status) VALUES (?,?,?,'verified')
                ON CONFLICT(event_id) DO UPDATE SET event_order=excluded.event_order,
                    occurred_at=excluded.occurred_at,order_status=excluded.order_status
                WHERE history_event_order.order_status!='verified'""",
                (event["event_id"], (origin["sequence_no"] - 1) * 1000, origin["created_at"]))

        for source in sources:
            payload, tool = source["payload"], source["payload"].get("tool")
            run = str(payload.get("run_id") or "")
            key = (source["trace_id"], run, tool)
            if source["event_type"].endswith("tool_call") and tool in {"Write", "Edit"}:
                queue[key].append(source)
                continue
            if not source["event_type"].endswith("tool_result"):
                continue
            if tool in {"Write", "Edit"}:
                call = queue[key].popleft() if queue[key] else None
                if not call or not wanted & {call["event_id"], source["event_id"]}:
                    continue
                if payload.get("success") is not True:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "file_operation_failed"})
                    continue
                params = call["payload"].get("params") or {}
                path = str(params.get("path") or "").replace("\\", "/")
                if not path or path.startswith(("/", ".")) or ".." in Path(path).parts or ":" in path:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "unsafe_or_missing_file_path"})
                    continue
                data = payload.get("data")
                result_path = str(data.get("path") or "") if isinstance(data, dict) else ""
                if isinstance(data, str):
                    match = re.match(r"(?:文件|文件已写入|文件内容未变化):\s*([^\n]+)", data)
                    result_path = match[1].strip() if match else ""
                if result_path.replace("\\", "/") != path:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "tool_pair_result_path_not_verified"})
                    continue
                owners = [unit for unit in units if unit["kind"] == "document" and unit["path"] == path
                          and owned(unit, source["trace_id"], run)]
                if len(owners) != 1:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "file_run_owner_not_unique"})
                    continue
                owner = owners[0]
                attempt = {"tool": tool, "params": params, "success": True, "result": data}
                matches = [event for event in events if event["history_id"] == owner["history_id"] and event["kind"] == "file_change_attempt"
                           and json.loads(event["content"]) == attempt]
                if len(matches) > 1:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "identical_attempts_need_sequence_disambiguation"})
                    continue
                if matches:
                    event = matches[0]
                else:
                    eid = uuid.uuid5(uuid.NAMESPACE_URL, "offline-history:" + owner["history_id"] + ":" + call["event_id"] + ":" + source["event_id"]).hex
                    event = {"event_id": eid, "history_id": owner["history_id"], "kind": "file_change_attempt",
                             "content": json.dumps(attempt, ensure_ascii=False)}
                    scope = {"path": path, "run_id": run, "trace_id": source["trace_id"], "trace_sequence_no": source["sequence_no"],
                             "source_event_ids": [call["event_id"], source["event_id"]], "provenance": "offline_exact_tool_pair"}
                    db.execute("""INSERT INTO history_events
                        (event_id,history_id,kind,content,revision_id,created_at,scope_json) VALUES (?,?,?,?,?,?,?)""",
                        (eid, owner["history_id"], event["kind"], event["content"], "", source["created_at"], json.dumps(scope)))
                    events.append(event)
                    report["events_added"] += 1
                attach(event, [call["event_id"], source["event_id"]], source)
            elif tool == "SubAgent" and source["event_id"] in wanted:
                call = by_id.get(source.get("parent_event_id"))
                children = starts[(source["trace_id"], source.get("parent_event_id"))]
                if not call or call["payload"].get("tool") != "SubAgent" or payload.get("success") is not True or len(children) != 1:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "dispatch_identity_not_verified"})
                    continue
                child = children[0]["payload"]
                preset = str(child.get("preset") or "")
                if not preset or preset != (call["payload"].get("params") or {}).get("preset"):
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "dispatch_preset_mismatch"})
                    continue
                owners = {unit["history_id"]: unit for unit in units if owned(unit, source["trace_id"], str(child.get("run_id") or ""))}
                matches = [event for event in events if event["history_id"] in owners and event["kind"] == preset + "_output"
                           and event["content"] == payload.get("data")]
                if preset == "reviewer":
                    matches = [event for event in matches if owners[event["history_id"]]["kind"] == "review"]
                if not matches:
                    report["unresolved"].append({"event_id": source["event_id"], "reason": "exact_run_output_history_not_found"})
                    continue
                for event in matches:
                    attach(event, [source["event_id"]], source)
        report["sources_mapped"] = len(mapped)
        report["remaining_event_ids"] = sorted(wanted - mapped)
        db.commit()
    return report
