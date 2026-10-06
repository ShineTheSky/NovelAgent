"""Recover legacy History chronology without changing original evidence or memories."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from novelagent.history.backfill import _requests
from novelagent.history.legacy_tools import normalize_tool_events
from novelagent.history.store import DocumentHistoryStore


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _key(value):
    return json.dumps(_json(value), ensure_ascii=False, sort_keys=True)


def _source_evidence(request):
    """Build ordered candidates from retained evidence, not from the current file."""
    candidates, thinking = [], defaultdict(list)
    pending, questions = defaultdict(list), {}
    presets, writes = {}, []

    def add(kind, content, index, source, run="main", aliases=None):
        if not content:
            return None
        row = {"kind": kind, "content": content, "order": index * 1000,
               "occurred_at": source.get("created_at") or "", "run": run,
               "sources": aliases or [source["event_id"]], "parts": []}
        candidates.append(row)
        return row

    for index, source in enumerate(normalize_tool_events(request["events"])):
        kind, payload = source["event_type"], source["payload"]
        run = str(payload.get("run_id") or "main")
        if kind.endswith("subagent_start"):
            presets[run] = payload.get("preset") or ""
            task = payload.get("task_prompt") or payload.get("task_summary") or ""
            add("main_delegation", task, index, source, run)
            add("reviewer_delegation", task, index, source, run)
            add("subagent_task", task, index, source, run)
            # task_summary is a UI abbreviation of this same invocation, not
            # another demand/event. Prefer its full task_prompt when available.
        if kind == "user_message":
            add("user_input", payload.get("content") or "", index, source)
        elif kind == "assistant_turn":
            add("main_agent_thinking", payload.get("reasoning_content") or "", index, source)
            output = add("main_agent_output", payload.get("content") or "", index, source)
            if output:
                output["order"] += 1
        elif kind in {"subagent_thinking", "workflow_thinking"}:
            content = str(payload.get("content") or "")
            if content:
                thinking[run].append({"content": content, "order": index * 1000,
                                      "occurred_at": source.get("created_at") or "",
                                      "sources": [source["event_id"]]})
        elif kind.endswith("subagent_done"):
            preset = payload.get("preset") or presets.get(run) or "main_agent"
            output = str(payload.get("result") or "")
            add(f"{preset}_output", output, index, source, run)
            if output.startswith("无需修改："):
                add("evaluation_no_change", output, index, source, run)

        if kind.endswith("tool_call"):
            pending[(run, payload.get("tool"))].append((index, source))
            if payload.get("tool") == "SubAgent":
                params = payload.get("params") or {}
                add("main_delegation", params.get("task") or "", index, source, run)
                for offset, attachment in enumerate(params.get("attachments") or []):
                    if isinstance(attachment, dict) and attachment.get("path") and attachment.get("content"):
                        reference = add("document_reference", json.dumps({"params": {"path": attachment["path"]},
                                        "content": attachment["content"]}, ensure_ascii=False), index, source, run)
                        reference["order"] += 10 + offset
        if kind == "question_ask" or (kind.endswith("tool_call") and payload.get("tool") == "AskUserQuestion"):
            content = json.dumps(payload.get("questions", (payload.get("params") or {}).get("questions", [])), ensure_ascii=False)
            previous = questions.get(run)
            # A displayed question immediately following its call is an alias,
            # not a second question. Asking the same text later remains distinct.
            same_call = previous and previous.get("question_call") and (
                source.get("parent_event_id") == previous["sources"][0]
                or previous["order"] == (index - 1) * 1000)
            if kind == "question_ask" and same_call and _key(previous["content"]) == _key(content):
                previous["sources"].append(source["event_id"])
                previous["question_call"] = False
            else:
                questions[run] = add("question_ask", content, index, source, run)
                questions[run]["question_call"] = kind.endswith("tool_call")
        if kind == "user_answer":
            add("user_answer", json.dumps(payload.get("answers", payload), ensure_ascii=False), index, source, run)
            questions.pop(run, None)
        if kind.endswith("tool_result"):
            tool = payload.get("tool")
            queue = pending[(run, tool)]
            if payload.get("call_source_event_id"):
                call = next((item for item in queue if item[1]["event_id"] == payload["call_source_event_id"]
                             and item[1]["payload"].get("params") == payload.get("call_params")), None)
                if call:
                    queue.remove(call)
            else:
                call = queue.pop(0) if queue else None
            if not tool and payload.get("data"):
                add("unattributed_tool_result", json.dumps(payload, ensure_ascii=False), index, source, run)
            if tool == "SubAgent" and payload.get("call_params") and payload.get("data"):
                if not any(e["event_type"].endswith("subagent_done")
                           and e["payload"].get("result") == payload["data"] for e in request["events"]):
                    preset = payload["call_params"].get("preset") or "subagent"
                    add(f"{preset}_output", str(payload["data"]), index, source, run)
            if tool == "AskUserQuestion" and payload.get("success"):
                content = json.dumps(_json(payload.get("data")), ensure_ascii=False)
                previous = next((c for c in reversed(candidates) if c["kind"] == "user_answer" and c["run"] == run), None)
                same_answer = previous and (previous["order"] >= call[0] * 1000 if call else previous["order"] == (index - 1) * 1000)
                if same_answer and _key(previous["content"]) == _key(content):
                    previous["sources"].append(source["event_id"])
                else:
                    add("user_answer", content, index, source, run)
                questions.pop(run, None)
            if call and tool == "Read" and payload.get("success"):
                add("document_reference", json.dumps({"params": call[1]["payload"].get("params") or {},
                    "content": payload.get("data")}, ensure_ascii=False), index, source, run,
                    [call[1]["event_id"], source["event_id"]])
            if call and tool in {"Write", "Edit"}:
                writes.append((run, (call[1]["payload"].get("params") or {}).get("path"), index, source))
                add("file_change_attempt", json.dumps({"tool": tool, "params": call[1]["payload"].get("params") or {},
                    "success": payload.get("success"), "result": _json(payload.get("data") if payload.get("success") else payload.get("error"))},
                    ensure_ascii=False), index, source, run, [call[1]["event_id"], source["event_id"]])
        for offset, ref in enumerate(payload.get("revision_events") or []):
            if isinstance(ref, dict) and ref.get("revision_id"):
                write = next((w for w in reversed(writes) if w[0] == run and w[1] == ref.get("path")
                              and ref["revision_id"] in str(w[3]["payload"].get("data") or "")), None)
                position, origin = (write[2], write[3]) if write else (index, source)
                reference = add("revision_reference", json.dumps(ref, ensure_ascii=False), position, origin, run)
                reference["order"] += 10 + offset * 2
                revision = add("revision", str(ref["revision_id"]), position, origin, run)
                revision["order"] += 11 + offset * 2

    for run, parts in thinking.items():
        candidate = {"kind": f"{presets.get(run) or 'main_agent'}_thinking",
                     "content": "".join(part["content"] for part in parts), "run": run,
                     "order": parts[0]["order"], "occurred_at": parts[0]["occurred_at"],
                     "sources": [sid for part in parts for sid in part["sources"]], "parts": parts}
        candidates.append(candidate)
    return candidates


def _matches(event, candidate):
    if event["kind"] != candidate["kind"]:
        return False
    if event["kind"] == "file_change_attempt":
        left, right = _json(event["content"]), _json(candidate["content"])
        return isinstance(left, dict) and all(_key(left.get(key)) == _key(right.get(key))
                                              for key in ("tool", "params", "success", "result"))
    if event["kind"] == "revision_reference":
        left, right = _json(event["content"]), _json(candidate["content"])
        return isinstance(left, dict) and left.get("revision_id") == right.get("revision_id")
    return _key(event["content"]) == _key(candidate["content"])


def repair_history_order(trace_db: Path, project: Path, *, apply=False) -> dict:
    project = Path(project).resolve()
    store = DocumentHistoryStore(project)
    report = {"mode": "apply" if apply else "dry_run", "requests": 0, "histories": 0,
              "ordered": 0, "split_parts": 0, "duplicates": 0, "unverified": [], "changed": 0}
    with closing(sqlite3.connect(f"file:{Path(trace_db).resolve().as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        requests = [r for r in _requests(db, project.name) if r["status"] in {"completed", "failed", "interrupted"}]
    if not store.db_path.exists():
        return report
    # Dry runs do not initialize schemas or write to either database.
    with closing(sqlite3.connect(f"file:{store.db_path.as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        units = [dict(r) for r in db.execute("SELECT * FROM history_units ORDER BY rowid")]
        events = [dict(r) for r in db.execute("SELECT * FROM history_events ORDER BY rowid")]
        sources = defaultdict(list)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_source_events'").fetchone():
            for row in db.execute("SELECT * FROM history_source_events"):
                sources[row["event_id"]].append(row["source_event_id"])
        old_order = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM history_event_order")} if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='history_event_order'").fetchone() else {}
    planned, additions = {}, {}
    for request in requests:
        owned = {u["history_id"]: u for u in units if u["status"] != "collecting" and
                 (u["request_id"] == request["request_id"] or u["parent_request_id"] == request["request_id"]
                  or u["request_id"].startswith(request["request_id"] + ":run:"))}
        if not owned:
            continue
        report["requests"] += 1
        report["histories"] += len(owned)
        candidates = _source_evidence(request)
        by_source = defaultdict(list)
        for candidate in candidates:
            for sid in candidate["sources"]:
                by_source[sid].append(candidate)
        consumed, canonical = defaultdict(set), {}
        # Prefer already-canonical provenance IDs over archived requirement copies.
        for event in sorted(events, key=lambda event: not bool(sources[event["event_id"]])):
            if not event["content"] and old_order.get(event["event_id"], {}).get("canonical_event_id"):
                continue  # Storage aliases must not become visible unverified evidence.
            if event["history_id"] not in owned or event["event_id"] in old_order and old_order[event["event_id"]].get("original_event_id"):
                continue
            unit = owned[event["history_id"]]
            scope = json.loads(event.get("scope_json") or "{}")
            run = scope.get("run_id") or (unit["request_id"].split(":run:", 1)[1] if ":run:" in unit["request_id"] else "main")
            mapped = [c for sid in sources[event["event_id"]] for c in by_source[sid] if _matches(event, c)]
            matches = [c for c in candidates if _matches(event, c) and
                       (c["run"] == run or event["kind"].startswith("reviewer_") or event["kind"] in {"user_input", "user_answer", "question_ask"})]
            if event["kind"].startswith("reviewer_") and len({c["run"] for c in matches}) > 1:
                matches = [c for c in matches if c["run"] == run]
            candidate = (mapped[0] if mapped and len({c["order"] for c in mapped}) == 1 else
                         next((c for c in mapped or matches
                               if c["order"] not in consumed[(event["history_id"], event["kind"])]), None))
            if candidate is None and len({c["order"] for c in matches}) == 1:
                candidate = matches[0]  # A copied delegation/answer, not a new occurrence.
            if not candidate:
                planned[event["event_id"]] = {"event_id": event["event_id"], "event_order": None, "occurred_at": "",
                    "order_status": "unverified", "canonical_event_id": "", "original_event_id": ""}
                report["unverified"].append({"history_id": event["history_id"], "event_id": event["event_id"], "kind": event["kind"]})
                continue
            consumed[(event["history_id"], event["kind"])].add(candidate["order"])
            metadata = {"event_id": event["event_id"], "event_order": candidate["order"], "occurred_at": candidate["occurred_at"],
                        "order_status": "verified" if candidate["occurred_at"] else "sequence_only",
                        "canonical_event_id": "", "original_event_id": ""}
            identity = (event["history_id"], event["kind"], candidate["order"])
            if identity in canonical:
                metadata.update(order_status="duplicate", canonical_event_id=canonical[identity])
                report["duplicates"] += 1
            else:
                canonical[identity] = event["event_id"]
                if len(candidate["parts"]) > 1:
                    metadata["order_status"] = "superseded"
                    for part in candidate["parts"]:
                        eid = f"{event['event_id']}:part:{part['sources'][0]}"
                        scope = _json(event.get("scope_json") or "{}")
                        additions[eid] = {**event, "event_id": eid, "content": part["content"],
                                          "scope_json": json.dumps({**scope, "run_id": candidate["run"], "evidence_scope": "run"}, ensure_ascii=False)}
                        planned[eid] = {"event_id": eid, "event_order": part["order"], "occurred_at": part["occurred_at"],
                            "order_status": "verified" if part["occurred_at"] else "sequence_only",
                            "canonical_event_id": "", "original_event_id": event["event_id"]}
                        report["split_parts"] += 1
                report["ordered"] += 1
            planned[event["event_id"]] = metadata
    existing = {event["event_id"]: event for event in events}
    changed = {eid for eid, value in planned.items() if old_order.get(eid) != value}
    changed.update(eid for eid, event in additions.items() if eid not in existing or existing[eid].get("scope_json") != event["scope_json"])
    report["changed"] = len(changed)
    if apply and planned:
        with store._connect() as db:
            for event in additions.values():
                db.execute("INSERT OR IGNORE INTO history_events (event_id,history_id,kind,content,revision_id,scope_json) VALUES (?,?,?,?,?,?)",
                    (event["event_id"],event["history_id"],event["kind"],event["content"],event["revision_id"],event.get("scope_json") or "{}"))
                db.execute("UPDATE history_events SET scope_json=? WHERE event_id=?", (event["scope_json"], event["event_id"]))
            for value in planned.values():
                db.execute("INSERT OR REPLACE INTO history_event_order VALUES (:event_id,:event_order,:occurred_at,:order_status,:canonical_event_id,:original_event_id)", value)
    return report
