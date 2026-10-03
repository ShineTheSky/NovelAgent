"""Bind derived records to retained History evidence without extracting new claims."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import yaml

from novelagent.history.backfill import _requests, _run_documents
from novelagent.history.store import DocumentHistoryStore
from novelagent.history.sources import normalize_history_roots


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def map_trace_sources(trace_db: Path, project: Path, source_ids: set[str]) -> dict[str, list[dict]]:
    """Return exact evidence links, or explicitly weaker file/run context links.

    Trace turn numbers are not session turn numbers. Original event IDs locate
    the owning request first; History is then scoped to that request and run.
    Both databases are opened read-only, without invoking History migrations.
    """
    project = Path(project).resolve()
    with closing(sqlite3.connect(f"file:{Path(trace_db).resolve().as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        requests = [r for r in _requests(db) if r["project_id"] == project.name]
    with closing(sqlite3.connect(f"file:{(project / '.history/history.sqlite3').as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        units = [dict(row) for row in db.execute(
            "SELECT * FROM history_units WHERE kind IN ('document', 'conversation', 'requirement', 'review') ORDER BY rowid")]
        events = defaultdict(list)
        for row in db.execute("SELECT * FROM history_events ORDER BY rowid"):
            events[row["history_id"]].append(dict(row))
        canonical = defaultdict(list)
        event_orders = {row["event_id"]: row["event_order"] for row in db.execute("SELECT event_id,event_order FROM history_event_order")} if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'history_event_order'").fetchone() else {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'history_source_events'").fetchone():
            for row in db.execute("SELECT s.source_event_id, e.event_id, e.history_id FROM history_source_events s JOIN history_events e ON e.event_id = s.event_id"):
                if row["history_id"] in {u["history_id"] for u in units}:
                    canonical[row["source_event_id"]].append(dict(row))
    evidence = {u["history_id"]: [*events[u["history_id"]],
                *events[DocumentHistoryStore._unit_id(u["request_id"], "request")]] for u in units}
    result = {sid: [] for sid in sorted(source_ids)}
    for request in requests:
        candidates = [u for u in units if (
            u["request_id"] == request["request_id"]
            or u["request_id"].startswith(request["request_id"] + ":run:")
            or u.get("parent_request_id") == request["request_id"]
        ) and u["session_id"] == request["session_id"]
            and u["session_turn_no"] == request["turn_no"]
            and (u["kind"] != "requirement" or u["request_id"] == request["request_id"])]
        # Associate a main SubAgent result with its actual run, not every run
        # that happens to have returned the same wording.
        pending_spawns, spawn_runs, result_runs = [], {}, {}
        for event in request["events"]:
            payload = event["payload"]
            if event["event_type"] == "tool_call" and payload.get("tool") == "SubAgent":
                pending_spawns.append(event["event_id"])
            elif event["event_type"] == "subagent_subagent_start" and pending_spawns:
                spawn_runs.setdefault(pending_spawns[0], str(payload.get("run_id") or ""))
            elif event["event_type"] == "tool_result" and payload.get("tool") == "SubAgent" and pending_spawns:
                result_runs[event["event_id"]] = spawn_runs.get(pending_spawns.pop(0), "")
        for source_index, source in enumerate(request["events"]):
            sid = source["event_id"]
            if sid not in source_ids:
                continue
            if canonical[sid]:
                result[sid] = [{"source_event_id": sid, "history_id": row["history_id"], "history_event_ids": [row["event_id"]], "match": "evidence"} for row in canonical[sid]]
                continue
            payload, kind = source["payload"], source["event_type"]
            tool, run = payload.get("tool"), str(payload.get("run_id") or "main")
            for unit in candidates:
                hits = []
                for event in evidence[unit["history_id"]]:
                    ek, content = event["kind"], event["content"]
                    matched = False
                    if kind == "user_message" and ek == "user_input":
                        original = str(payload.get("content") or "")
                        matched = bool(original) and (content == original or content.startswith(original + "\n"))
                    elif kind == "user_answer" and ek == "user_answer":
                        matched = _json(content) == payload.get("answers", payload)
                    elif kind.endswith("tool_result") and tool == "AskUserQuestion" and ek == "user_answer":
                        matched = _json(content) == _json(payload.get("data"))
                    elif kind == "assistant_turn" and unit["request_id"] == request["request_id"]:
                        expected = {"main_agent_thinking": payload.get("reasoning_content"), "main_agent_output": payload.get("content")}
                        matched = bool(expected.get(ek)) and content == expected[ek]
                        if matched and event["event_id"] in event_orders:
                            matched = event_orders[event["event_id"]] == source_index * 1000 + (1 if ek == "main_agent_output" else 0)
                    elif kind == "tool_call" and tool == "SubAgent" and unit["request_id"] == request["request_id"]:
                        matched = ek == "main_delegation" and content == (payload.get("params") or {}).get("task")
                        if matched and event["event_id"] in event_orders:
                            matched = event_orders[event["event_id"]] == source_index * 1000
                    elif kind == "tool_result" and tool == "SubAgent" and result_runs.get(sid):
                        matched = (unit["request_id"] == DocumentHistoryStore.run_request_id(request["request_id"], result_runs[sid])
                                   and ek.endswith("_output") and content == payload.get("data"))
                    elif tool in {"Edit", "Write"} and ek == "file_change_attempt":
                        change = _json(content)
                        if (isinstance(change, dict) and change.get("tool") == tool
                                and str(change.get("run_id") or "main") == run):
                            if kind.endswith("tool_call"):
                                matched = change.get("params") == payload.get("params")
                            elif kind.endswith("tool_result"):
                                value = payload.get("data") if payload.get("success") else payload.get("error")
                                matched = _json(change.get("result")) == _json(value)
                    if matched:
                        hits.append(event["event_id"])
                same_file = (tool == "Read" and kind.endswith("tool_call")
                             and unit["path"] == (payload.get("params") or {}).get("path"))
                same_run = run != "main" and unit["request_id"] == DocumentHistoryStore.run_request_id(request["request_id"], run)
                if hits or same_file or same_run:
                    result[sid].append({"source_event_id": sid, "history_id": unit["history_id"],
                                        "history_event_ids": list(dict.fromkeys(hits)),
                                        "match": "evidence" if hits else "context"})
            # Prefer retained evidence to same-run candidates for other files.
            if any(link["match"] == "evidence" for link in result[sid]):
                result[sid] = [link for link in result[sid] if link["match"] == "evidence"]
    return result


def link_records(trace_db: Path, project: Path, *, layers=("insight", "memory", "pattern"),
                 apply: bool = False, replace_legacy_links: bool = False) -> dict:
    """Append provenance only. Preserve record text, rankings, and Trace refs."""
    if not layers or set(layers) - {"insight", "memory", "pattern"}:
        raise ValueError("Unsupported record layer")
    project = Path(project).resolve()
    records, memories = [], {}
    for layer in ("insight", "memory", "pattern"):
        for path in sorted((project / f".{layer}").glob("*/*.md")):
            try:
                _, header, body = path.read_text(encoding="utf-8").split("---", 2)
                record = yaml.safe_load(header)
            except (ValueError, yaml.YAMLError):
                continue
            # Old Evidence IDs and records moved between layers keep their IDs.
            if not isinstance(record, dict) or not record.get("id"):
                continue
            ids = set(record.get("source_event_ids") or [])
            ids.update(record.get("history_source_decisions") or {})
            for ref in record.get("trace_refs") or []:
                ids.update(ref.get("source_event_ids") or [])
            if layer == "memory":
                memories[record["id"]] = (record, ids)
            if layer in layers:
                records.append((layer, path, record, body, ids))
    sources = {sid for _, _, _, _, ids in records for sid in ids}
    for layer, _, record, _, _ in records:
        if layer == "pattern":
            for memory_id in record.get("memory_ids") or []:
                if memory_id in memories:
                    sources.update(memories[memory_id][1])
    links = map_trace_sources(trace_db, project, sources)
    report = {"mode": "apply" if apply else "dry_run", "records": len(records),
              "linked": 0, "changed": 0, "layers": {layer: {"records": 0, "linked": 0, "changed": 0} for layer in layers}, "items": []}
    for layer, path, record, body, ids in records:
        inherited = [memories[mid][0] for mid in record.get("memory_ids", []) if layer == "pattern" and mid in memories]
        if layer == "pattern":
            ids = ids | {sid for mid in record.get("memory_ids", []) if mid in memories for sid in memories[mid][1]}
        mapped = [link for sid in sorted(ids) for link in links[sid]]
        decisions = record.get("history_source_decisions") or {}
        mapped = [{**link, "match": decisions.get(link["source_event_id"], {}).get("match", link["match"])}
                  for link in mapped if not decisions.get(link["source_event_id"], {}).get("history_id")
                  or link["history_id"] == decisions[link["source_event_id"]]["history_id"]]
        old_links = record.get("history_source_links") or []
        old_histories = {link["history_id"] for link in old_links} if replace_legacy_links else set()
        old_events = {eid for link in old_links for eid in link["history_event_ids"]} if replace_legacy_links else set()
        history_ids = list(dict.fromkeys([*(hid for hid in record.get("history_ids", []) if hid not in old_histories),
                                         *(hid for source in inherited for hid in source.get("history_ids", [])),
                                         *(link["history_id"] for link in mapped if link["match"] == "evidence")]))
        event_ids = list(dict.fromkeys([*(eid for eid in record.get("history_event_ids", []) if eid not in old_events),
                                       *(eid for source in inherited for eid in source.get("history_event_ids", [])),
                                       *(eid for link in mapped if link["match"] == "evidence" for eid in link["history_event_ids"])]))
        source_links = [] if replace_legacy_links else list(old_links)
        source_links.extend(link for source in inherited for link in source.get("history_source_links", []) if link not in source_links)
        source_links.extend(link for link in mapped if link not in source_links)
        update = {"history_ids": history_ids, "history_event_ids": event_ids,
                  "history_source_links": source_links}
        if any(link["match"] == "context" for link in mapped) or any(source.get("related_history_ids") for source in inherited):
            update["related_history_ids"] = list(dict.fromkeys([*record.get("related_history_ids", []),
                *(hid for source in inherited for hid in source.get("related_history_ids", [])),
                *(link["history_id"] for link in mapped if link["match"] == "context")]))
        update.update(normalize_history_roots(project, {**record, **update}))
        history_ids = update["history_ids"]
        changed = bool(mapped or history_ids or update.get("related_history_ids") or inherited) and any(record.get(key) != value for key, value in update.items())
        linked = bool(history_ids or update.get("related_history_ids"))
        report["linked"] += linked
        report["changed"] += changed
        report["layers"][layer]["records"] += 1
        report["layers"][layer]["linked"] += linked
        report["layers"][layer]["changed"] += changed
        report["items"].append({"id": record["id"], "layer": layer, "history_ids": history_ids,
                                "missing_memory_ids": [mid for mid in record.get("memory_ids", []) if layer == "pattern" and mid not in memories],
                                "context_only_source_event_ids": [sid for sid in sorted(ids)
                                    if links[sid] and not any(link["match"] == "evidence" for link in links[sid])],
                                "unmapped_source_event_ids": sorted(sid for sid in ids if not links[sid])})
        if apply and changed:
            record.update(update)
            content = "---\n" + yaml.safe_dump(record, allow_unicode=True, sort_keys=False).rstrip() + "\n---" + body
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return report


def link_memories(trace_db: Path, project: Path, *, apply: bool = False, replace_legacy_links: bool = False) -> dict:
    report = link_records(trace_db, project, layers=("memory",), apply=apply, replace_legacy_links=replace_legacy_links)
    report["memories"] = report["records"]
    return report


def migrate_requirements(trace_db: Path, project: Path, *, apply: bool = False) -> dict:
    """Normalize existing Trace imports; never delete revisions or original evidence."""
    project = Path(project).resolve()
    store = DocumentHistoryStore(project)
    with closing(sqlite3.connect(f"file:{Path(trace_db).resolve().as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        requests = [r for r in _requests(db) if r["project_id"] == project.name]
    report = {"mode": "apply" if apply else "dry_run", "requirements": 0, "file_histories": 0, "missing": []}
    if not store.db_path.exists():
        return report
    with closing(sqlite3.connect(f"file:{store.db_path.as_posix()}?mode=ro", uri=True)) as db:
        present = {row[0] for row in db.execute("SELECT history_id FROM history_units")}
    for request in requests:
        root_id = store._unit_id(request["request_id"], "request")
        if root_id not in present:
            continue
        documents = _run_documents(request, project)
        targets = {(run, path): details for (run, path), details in documents.items()
                   if store._unit_id(store.run_request_id(request["request_id"], run), "document", path) in present}
        if not targets:
            continue
        report["requirements"] += 1
        report["file_histories"] += len(targets)
        if not apply:
            continue
        with store._connect() as db:
            db.execute("UPDATE history_units SET kind = 'requirement' WHERE history_id = ?", (root_id,))
            # Imported roots remain deferred: migration must not schedule an LLM run.
            db.execute("UPDATE history_units SET analysis_status = 'deferred' WHERE history_id = ? AND analysis_status = 'routed'", (root_id,))
        shared = []
        read_calls = []
        with store._connect() as db:
            existing_events = [dict(row) for row in db.execute("SELECT * FROM history_events WHERE history_id = ? ORDER BY rowid", (root_id,))]
            source_events = {row["source_event_id"]: row["event_id"] for row in db.execute(
                "SELECT s.* FROM history_source_events s JOIN history_events e ON e.event_id = s.event_id WHERE e.history_id = ?", (root_id,))}
        used_events = set()
        for source in request["events"]:
            payload, kind = source["payload"], source["event_type"]
            if kind == "tool_call" and payload.get("tool") == "Read":
                read_calls.append(source)
            elif kind == "tool_result" and payload.get("tool") == "Read" and read_calls:
                call = read_calls.pop(0)
                if payload.get("success"):
                    params = call["payload"].get("params") or {}
                    data = payload.get("data")
                    revision = re.search(r"(?:当前修订:\s*|revision_id:\s*)(rev_[^\s]+)", str(data or ""))
                    eid = f"{root_id}:source:{call['event_id']}"
                    store.append_request_event(request["request_id"], "document_reference", json.dumps({
                        "params": params, "path": params.get("path", ""),
                        "revision_id": revision.group(1) if revision else "", "content": data,
                    }, ensure_ascii=False), event_id=eid)
                    with store._connect() as db:
                        db.executemany("INSERT OR IGNORE INTO history_source_events VALUES (?, ?)",
                                       [(call["event_id"], eid), (source["event_id"], eid)])
            event_kind, content = "", ""
            if kind == "user_message" and payload.get("content") == request["user_input"]:
                event_kind, content = "user_input", request["user_input"]
            elif kind == "user_answer":
                event_kind, content = "user_answer", json.dumps(payload.get("answers", payload), ensure_ascii=False)
            elif kind.endswith("tool_result") and payload.get("tool") == "AskUserQuestion" and payload.get("success"):
                event_kind, content = "user_answer", json.dumps(_json(payload.get("data")), ensure_ascii=False)
            elif kind == "question_ask" or (kind.endswith("tool_call") and payload.get("tool") == "AskUserQuestion"):
                event_kind, content = "question_ask", json.dumps(payload.get("questions", (payload.get("params") or {}).get("questions", [])), ensure_ascii=False)
            if not event_kind:
                continue
            # A tool result and its promoted user_answer are the same answer.
            previous = next((row for row in reversed(shared) if row[1:3] == (event_kind, content)), None)
            alias = previous and event_kind == "user_answer" and (
                kind == "tool_result" or str(payload.get("source") or "").startswith(("subagent:", "workflow:")))
            event_id = source_events.get(source["event_id"])
            if not event_id:
                existing = next((e for e in existing_events if e["kind"] == event_kind
                                 and e["content"] == content and e["event_id"] not in used_events), None)
                event_id = previous[0] if alias else existing["event_id"] if existing else f"{root_id}:source:{source['event_id']}"
            used_events.add(event_id)
            source_events[source["event_id"]] = event_id
            store.append_request_event(request["request_id"], event_kind, content, event_id=event_id,
                scope=None if alias else {"run_id": payload.get("run_id", ""), "source": payload.get("source", "main"),
                                          "trace_id": source["trace_id"], "sequence_no": source["sequence_no"]})
            with store._connect() as db:
                db.execute("INSERT OR IGNORE INTO history_source_events VALUES (?, ?)", (source["event_id"], event_id))
            shared.append((event_id, event_kind, content))
        target_refs = []
        run_refs = defaultdict(list)
        for (run, path), details in targets.items():
            child = store.run_request_id(request["request_id"], run)
            event_ids = [eid for eid, kind, _ in shared if kind == "user_input"]
            event_ids.extend(source_events[sid] for sid in details["answer_source_event_ids"] if sid in source_events)
            target_refs.append((child, path, list(dict.fromkeys(event_ids))))
            run_refs[child].extend(event_ids)
        for child, event_ids in run_refs.items():
            store.set_requirement_refs(child, root_id, list(dict.fromkeys(event_ids)))
        for child, path, event_ids in target_refs:
            store.set_requirement_refs(child, root_id, event_ids, path=path)
    return report
