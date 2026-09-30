"""Deterministic, conservative import of legacy main-Agent Traces into History."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from novelagent.history import DocumentHistoryStore
from novelagent.versioning import revision_manager


_TRUNCATED = "[TRACE_PAYLOAD_TRUNCATED]"
_PATH = re.compile(r"(?:chapters|outlines|world|characters|reference)/[^\s\]\[()'\"，。；：]+\.md")
_REVISION = re.compile(r"当前修订:\s*(rev_[^\s]+)")


def _payload(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["payload_json"] or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _events(db: sqlite3.Connection, trace_id: str) -> list[dict]:
    return [{**dict(row), "payload": _payload(row)} for row in db.execute(
        "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY sequence_no", (trace_id,),
    )]


def _requests(db: sqlite3.Connection) -> list[dict]:
    traces = [dict(row) for row in db.execute(
        "SELECT * FROM traces WHERE source_agent = 'main_agent' "
        "AND source IN ('live', 'historical') ORDER BY started_at, rowid",
    )]
    groups: dict[tuple, list[tuple[dict, dict]]] = defaultdict(list)
    for trace in traces:
        if trace["source"] == "historical":
            for event in _events(db, trace["trace_id"]):
                no = event["payload"].get("turn_no")
                if no is not None:
                    groups[("historical", trace["trace_id"], no)].append((trace, event))
            continue
        # A linked Trace may be a later user turn, not just context compression.
        # Only segments with the same session turn belong to one request.
        key = (("live", trace["project_id"], trace["session_id"], trace["turn_no"])
               if trace["turn_no"] is not None else ("live", trace["trace_id"]))
        groups[key].extend((trace, event) for event in _events(db, trace["trace_id"]))

    requests = []
    for key, pairs in groups.items():
        if not pairs:
            continue
        members = list(dict.fromkeys(trace["trace_id"] for trace, _ in pairs))
        first, terminal = pairs[0][0], pairs[-1][0]
        request_id = (f"{first['trace_id']}:turn:{key[2]}"
                      if key[0] == "historical" else first["trace_id"])
        user = next((str(event["payload"].get("content") or "") for _, event in pairs
                     if event["event_type"] == "user_message"
                     and not str(event["payload"].get("content") or "").startswith(
                         ("[context_compression]", "[manual_context_compression]"))), "")
        if not user:
            user = next((str(trace["user_message"] or "") for trace, _ in pairs
                         if not str(trace["user_message"] or "").startswith(
                             ("[context_compression]", "[manual_context_compression]"))), "")
        answer = next((str(event["payload"].get("content") or "") for _, event in reversed(pairs)
                      if event["event_type"] == "assistant_turn"), "")
        turn_no = (int(key[2]) if key[0] == "historical" else
                   int(first["turn_no"] or 0))
        requests.append({
            "request_id": request_id, "project_id": terminal["project_id"],
            "session_id": terminal["session_id"], "status": terminal["status"],
            "user_input": user or str(first["user_message"] or ""),
            "answer": answer or str(terminal["final_answer"] or ""),
            "trace_ids": members, "turn_no": turn_no,
            "started_at": first["started_at"],
            "events": [event for _, event in pairs],
        })
    return requests


def _safe_path(project_dir: Path, raw: object) -> str:
    path = str(raw or "").replace("\\", "/")
    if not path.endswith(".md") or path.startswith("/") or ":" in path:
        return ""
    resolved = (project_dir / path).resolve()
    if not resolved.is_relative_to(project_dir.resolve()):
        return ""
    relative = resolved.relative_to(project_dir.resolve())
    return relative.as_posix() if not set(relative.parts) & {".git", ".history", ".memory", ".insight", ".pattern"} else ""


def _extract(request: dict, project_dir: Path) -> tuple[list[tuple[str, str]], dict[str, list[tuple[str, str]]], dict[str, list[dict]], int]:
    global_events: list[tuple[str, str]] = []
    documents: dict[str, list[tuple[str, str]]] = defaultdict(list)
    revisions: dict[str, list[dict]] = defaultdict(list)
    runs: dict[str, dict] = defaultdict(lambda: {"targets": set(), "thinking": [], "outputs": [], "task": "", "preset": ""})
    truncated = 0
    pending: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for event in request["events"]:
        kind, payload = event["event_type"], event["payload"]
        if _TRUNCATED in json.dumps(payload, ensure_ascii=False):
            truncated += 1
        run_id = str(payload.get("run_id") or "")
        if kind == "user_answer":
            global_events.append(("user_answer", json.dumps(payload.get("answers", payload), ensure_ascii=False)))
        elif kind == "assistant_turn":
            if payload.get("reasoning_content"):
                global_events.append(("main_agent_thinking", str(payload["reasoning_content"])))
            if payload.get("content"):
                global_events.append(("main_agent_output", str(payload["content"])))
        elif kind == "tool_call" and payload.get("tool") == "SubAgent":
            task = str((payload.get("params") or {}).get("task") or "")
            if task:
                global_events.append(("main_delegation", task))
        elif kind == "subagent_subagent_start" and run_id:
            run = runs[run_id]
            run["preset"] = str(payload.get("preset") or "")
            run["task"] = str(payload.get("task_prompt") or payload.get("task_summary") or "")
        elif kind == "subagent_thinking" and run_id:
            runs[run_id]["thinking"].append(str(payload.get("content") or ""))
        elif kind == "subagent_subagent_done" and run_id:
            run = runs[run_id]
            run["preset"] = str(payload.get("preset") or run["preset"])
            run["outputs"].append(str(payload.get("result") or ""))

        if kind in {"tool_call", "subagent_tool_call"} and payload.get("tool") in {"Write", "Edit"}:
            params = payload.get("params") or {}
            path = _safe_path(project_dir, params.get("path"))
            if path:
                if run_id:
                    runs[run_id]["targets"].add(path)
                pending[(run_id, str(payload["tool"]))].append({"path": path, "params": params})
        elif kind in {"tool_result", "subagent_tool_result"} and payload.get("tool") in {"Write", "Edit"}:
            queue = pending[(run_id, str(payload["tool"]))]
            if queue:
                call = queue.pop(0)
                documents[call["path"]].append(("file_change_attempt", json.dumps({
                    "tool": payload["tool"], "params": call["params"],
                    "success": bool(payload.get("success")),
                    "result": payload.get("data") if payload.get("success") else payload.get("error"),
                    "run_id": run_id,
                }, ensure_ascii=False)))

        for item in payload.get("revision_events") or []:
            if not isinstance(item, dict):
                continue
            path = _safe_path(project_dir, item.get("path"))
            if path and item.get("revision_id"):
                revisions[path].append(item)
                documents[path].append(("revision_reference", json.dumps(item, ensure_ascii=False)))
                if run_id:
                    runs[run_id]["targets"].add(path)

    for run in runs.values():
        if not run["targets"]:
            matches = _PATH.findall(run["task"])
            if matches:
                path = _safe_path(project_dir, matches[0])
                if path:
                    run["targets"].add(path)
        for path in run["targets"]:
            if run["task"]:
                documents[path].append(("main_delegation", run["task"]))
            if run["thinking"]:
                documents[path].append((f"{run['preset']}_thinking", "".join(run["thinking"])))
            for output in run["outputs"]:
                if output:
                    documents[path].append((f"{run['preset']}_output", output))
        if not run["targets"]:
            if run["task"]:
                global_events.append(("subagent_task", run["task"]))
            if run["thinking"]:
                global_events.append((f"{run['preset']}_thinking", "".join(run["thinking"])))
            for output in run["outputs"]:
                if output:
                    global_events.append((f"{run['preset']}_output", output))
    for queue in pending.values():
        for call in queue:
            documents[call["path"]].append(("file_change_attempt", json.dumps({
                "params": call["params"], "success": None, "result": "Trace has no matching tool result",
            }, ensure_ascii=False)))
    return global_events, documents, revisions, truncated


def _check_saved(store: DocumentHistoryStore, request: dict,
                 global_events: list[tuple[str, str]],
                 documents: dict[str, list[tuple[str, str]]],
                 verified: dict) -> list[str]:
    problems = []
    request_unit = store.get_unit(store._unit_id(request["request_id"], "request"))
    if not request_unit:
        return ["request unit missing"]
    if (request_unit["assistant_response"] != request["answer"]
            or request_unit["trace_ids"] != request["trace_ids"]
            or request_unit["status"] != request["status"]
            or int(request_unit["session_turn_no"]) != request["turn_no"]):
        problems.append("request metadata differs from Trace")
    expected_global = Counter([("user_input", request["user_input"]), *global_events])
    actual_global = Counter((e["kind"], e["content"]) for e in request_unit["events"])
    if expected_global - actual_global:
        problems.append("request evidence missing or changed")
    run_documents = None
    for path, expected in documents.items():
        unit = store.get_unit(store._unit_id(request["request_id"], "document", path))
        if not unit:
            if run_documents is None:
                run_documents = _run_documents(request, store.project_dir)
            matching = [(run_id, details) for (run_id, target), details in run_documents.items()
                        if target == path]
            if not matching:
                problems.append(f"document unit missing: {path}")
                continue
            for run_id, details in matching:
                child = store.get_unit(store._unit_id(
                    store.run_request_id(request["request_id"], run_id), "document", path))
                if not child:
                    problems.append(f"Agent run unit missing: {run_id}:{path}")
                    continue
                actual = Counter((e["kind"], e["content"]) for e in child["events"]
                                 if e["history_id"] == child["history_id"])
                if Counter(details["events"]) - actual:
                    problems.append(f"Agent run evidence missing: {run_id}:{path}")
                if details["refs"] and child["final_revision_id"] != details["refs"][-1]["revision_id"]:
                    problems.append(f"Agent run final revision differs: {run_id}:{path}")
            if path in verified:
                info, items = verified[path]
                if str(items[-1]["revision_id"]) == info.revision_id:
                    revision = store.get_revision(info.revision_id)
                    if not revision or revision["body_md"] != info.body:
                        problems.append(f"verified current revision differs: {path}")
            continue
        actual = Counter((e["kind"], e["content"]) for e in unit["events"]
                         if e["history_id"] == unit["history_id"])
        if Counter(expected) - actual:
            problems.append(f"document evidence missing or changed: {path}")
        if path in verified:
            info, items = verified[path]
            if str(items[-1]["revision_id"]) == info.revision_id:
                revision = store.get_revision(info.revision_id)
                if (unit["final_revision_id"] != info.revision_id or not revision
                        or revision["body_md"] != info.body):
                    problems.append(f"verified current revision differs: {path}")
    return problems


def _append_missing(store: DocumentHistoryStore, request_id: str,
                    global_events: list[tuple[str, str]],
                    documents: dict[str, list[tuple[str, str]]]) -> int:
    added = 0
    request_unit = store.get_unit(store._unit_id(request_id, "request"))
    present = Counter((e["kind"], e["content"]) for e in request_unit["events"])
    for item in global_events:
        if present[item]:
            present[item] -= 1
        else:
            store.append_request_event(request_id, *item)
            added += 1
    for path, entries in documents.items():
        unit = store.get_unit(store._unit_id(request_id, "document", path))
        if not unit:
            continue
        present = Counter((e["kind"], e["content"]) for e in unit["events"]
                          if e["history_id"] == unit["history_id"])
        for item in entries:
            if present[item]:
                present[item] -= 1
            else:
                store.append_document_event(request_id, path, *item)
                added += 1
    return added


def _run_documents(request: dict, project_dir: Path) -> dict[tuple[str, str], dict]:
    """Split a legacy request by modifying Agent run and target file."""
    runs: dict[str, dict] = defaultdict(lambda: {
        "task": "", "preset": "", "parent_run_id": "", "thinking": [],
        "output": "", "user_input": "", "trace_ids": [], "created_at": "",
        "targets": defaultdict(lambda: {"events": [], "refs": []}),
    })
    pending: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in request["events"]:
        kind, payload = event["event_type"], event["payload"]
        run_id = str(payload.get("run_id") or "main")
        run = runs[run_id]
        if run_id != "main" or kind in {"tool_call", "tool_result"}:
            if event["trace_id"] not in run["trace_ids"]:
                run["trace_ids"].append(event["trace_id"])
            if not run["created_at"]:
                run["created_at"] = event.get("created_at") or request["started_at"]
        if kind in {"subagent_subagent_start", "workflow_subagent_start"}:
            run["task"] = str(payload.get("task_prompt") or payload.get("task_summary") or "")
            run["preset"] = str(payload.get("preset") or "")
            run["parent_run_id"] = str(payload.get("parent_run_id") or "")
        elif kind in {"subagent_thinking", "workflow_thinking"}:
            run["thinking"].append(str(payload.get("content") or ""))
        elif kind in {"subagent_subagent_done", "workflow_subagent_done"}:
            run["output"] = str(payload.get("result") or "")
            run["preset"] = str(payload.get("preset") or run["preset"])
            run["user_input"] = str(payload.get("history_user_input") or "")

        if kind in {"tool_call", "subagent_tool_call", "workflow_tool_call"} and payload.get("tool") in {"Write", "Edit"}:
            params = payload.get("params") or {}
            path = _safe_path(project_dir, params.get("path"))
            if path:
                pending[(run_id, str(payload["tool"]))].append({"path": path, "params": params})
        elif kind in {"tool_result", "subagent_tool_result", "workflow_tool_result"} and payload.get("tool") in {"Write", "Edit"}:
            queue = pending[(run_id, str(payload["tool"]))]
            if queue:
                call = queue.pop(0)
                runs[run_id]["targets"][call["path"]]["events"].append(("file_change_attempt", json.dumps({
                    "tool": payload["tool"], "params": call["params"],
                    "success": bool(payload.get("success")),
                    "result": payload.get("data") if payload.get("success") else payload.get("error"),
                    "run_id": run_id,
                }, ensure_ascii=False)))

        for item in payload.get("revision_events") or []:
            if isinstance(item, dict):
                path = _safe_path(project_dir, item.get("path"))
                if path and item.get("revision_id"):
                    target = run["targets"][path]
                    target["refs"].append(item)
                    target["events"].append(("revision_reference", json.dumps(item, ensure_ascii=False)))

    for (run_id, tool), queue in pending.items():
        for call in queue:
            runs[run_id]["targets"][call["path"]]["events"].append(("file_change_attempt", json.dumps({
                "tool": tool, "params": call["params"], "success": None,
                "result": "Trace has no matching tool result", "run_id": run_id,
            }, ensure_ascii=False)))

    for reviewer in runs.values():
        if reviewer["preset"] != "reviewer" or not reviewer["output"]:
            continue
        reviewer_paths = {_safe_path(project_dir, match) for match in _PATH.findall(reviewer["task"])}
        for polisher in runs.values():
            if (polisher["preset"] != "chapter_polisher"
                    or polisher["parent_run_id"] != reviewer["parent_run_id"]):
                continue
            for path, target in polisher["targets"].items():
                if reviewer_paths and path not in reviewer_paths:
                    continue
                if reviewer["thinking"]:
                    target["events"].append(("reviewer_thinking", "".join(reviewer["thinking"])))
                target["events"].append(("reviewer_output", reviewer["output"]))
                if polisher["task"]:
                    target["events"].append(("reviewer_delegation", polisher["task"]))

    documents = {}
    for run_id, run in runs.items():
        if not run["targets"] and run["output"].startswith("无需修改："):
            matches = _PATH.findall(run["task"])
            if matches:
                path = _safe_path(project_dir, matches[0])
                if path:
                    run["targets"][path]["events"].append(("evaluation_no_change", run["output"]))
        for path, target in run["targets"].items():
            if run["thinking"]:
                target["events"].append((f"{run['preset'] or 'main_agent'}_thinking", "".join(run["thinking"])))
            if run["output"]:
                target["events"].append((f"{run['preset'] or 'main_agent'}_output", run["output"]))
            documents[(run_id, path)] = {**target, "task": run["task"], "preset": run["preset"],
                                          "user_input": run["user_input"] or request["user_input"],
                                          "trace_ids": run["trace_ids"] or request["trace_ids"],
                                          "created_at": run["created_at"] or request["started_at"]}
    return documents


def reorganize(trace_db: Path, workspace: Path, *, apply: bool = False,
               project_id: str = "") -> dict:
    """Replace legacy file aggregates with one History unit per run/file."""
    trace_db, workspace = Path(trace_db).resolve(), Path(workspace).resolve()
    db = sqlite3.connect(f"file:{trace_db.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    report = {"mode": "reorganize" if apply else "reorganize_dry_run",
              "requests": 0, "run_units": 0, "migrated": 0, "existing": 0,
              "repaired_events": 0, "removed_legacy_aggregates": 0, "errors": []}
    created_backups: set[Path] = set()
    try:
        for request in _requests(db):
            if project_id and request["project_id"] != project_id:
                continue
            project_dir = workspace / request["project_id"]
            if not project_dir.is_dir() or request["status"] not in {"completed", "failed", "interrupted"}:
                continue
            store = DocumentHistoryStore(project_dir)
            root = store.get_unit(store._unit_id(request["request_id"], "request"))
            if not root:
                continue
            documents = _run_documents(request, project_dir)
            if not documents:
                continue
            report["requests"] += 1
            report["run_units"] += len(documents)
            if not apply:
                continue
            backup = store.db_path.with_suffix(".pre-run-history.bak")
            if not backup.exists():
                with closing(sqlite3.connect(store.db_path)) as source, closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                created_backups.add(backup)
            try:
                for (run_id, path), details in documents.items():
                    child_id = store.run_request_id(request["request_id"], run_id)
                    history_id = store._unit_id(child_id, "document", path)
                    if store.get_unit(history_id):
                        report["existing"] += 1
                        with store._connect() as history_db:
                            history_db.execute("""UPDATE history_events SET content = ?
                                WHERE event_id = ?""", (details["user_input"],
                                f"{store._unit_id(child_id, 'request')}:input"))
                        present = Counter((event["kind"], event["content"])
                                          for event in store.get_unit(history_id)["events"]
                                          if event["history_id"] == history_id)
                        for kind, content in details["events"]:
                            if present[(kind, content)]:
                                present[(kind, content)] -= 1
                            else:
                                store.append_document_event(child_id, path, kind, content)
                                report["repaired_events"] += 1
                        continue
                    store.start_request(child_id, request["session_id"], details["user_input"],
                                        parent_request_id=request["request_id"])
                    if details["task"]:
                        store.append_request_event(child_id, "main_delegation", details["task"])
                    for kind, content in details["events"]:
                        store.append_document_event(child_id, path, kind, content)
                    for ref in details["refs"]:
                        store.attach_revision(child_id, path, str(ref["revision_id"]),
                                              str(ref.get("parent_revision_id") or ""))
                    store.close_request(child_id, request["status"], details["events"][-1][1]
                                        if details["events"] else "", details["trace_ids"], request["turn_no"])
                    with store._connect() as history_db:
                        history_db.execute("UPDATE history_units SET created_at = ? WHERE history_id = ?",
                                           (details["created_at"], history_id))
                        history_db.execute("UPDATE history_units SET created_at = ? WHERE history_id = ?",
                                           (details["created_at"], store._unit_id(child_id, "request")))
                    store.set_analysis_status(history_id, "deferred")
                    report["migrated"] += 1
                with store._connect() as history_db:
                    old_ids = [row[0] for row in history_db.execute("""SELECT history_id FROM history_units
                        WHERE request_id = ? AND kind IN ('document', 'legacy_aggregate')""",
                        (request["request_id"],))]
                    for old_id in old_ids:
                        history_db.execute("DELETE FROM history_events WHERE history_id = ?", (old_id,))
                        history_db.execute("DELETE FROM history_units WHERE history_id = ?", (old_id,))
                    report["removed_legacy_aggregates"] += len(old_ids)
            except (OSError, ValueError, sqlite3.Error) as exc:
                report["errors"].append({"request_id": request["request_id"], "error": str(exc)})
    finally:
        db.close()
    if apply and not report["errors"]:
        for backup in created_backups:
            backup.unlink()
    return report


def backfill(trace_db: Path, workspace: Path, *, apply: bool = False, check: bool = False,
             repair: bool = False,
             project_id: str = "") -> dict:
    """Backfill raw evidence without LLM calls; never invent missing revision bodies."""
    trace_db, workspace = Path(trace_db).resolve(), Path(workspace).resolve()
    if not trace_db.is_file() or not workspace.is_dir():
        raise FileNotFoundError("Trace DB or workspace directory is missing")
    db = sqlite3.connect(f"file:{trace_db.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    report = {"mode": "repair" if repair else "check" if check else "apply" if apply else "dry_run",
              "requests": 0, "imported": 0, "repaired_events": 0,
              "existing": 0, "running": 0, "missing_project": 0, "document_units": 0,
              "revision_references": 0, "verified_current_revisions": 0,
              "unverified_revision_references": 0, "truncated_events": 0,
              "checked": 0, "errors": []}
    try:
        for request in _requests(db):
            if project_id and request["project_id"] != project_id:
                continue
            report["requests"] += 1
            if request["status"] not in {"completed", "failed", "interrupted"}:
                report["running"] += 1
                continue
            project_dir = workspace / request["project_id"]
            if not project_dir.is_dir() or not project_dir.resolve().is_relative_to(workspace):
                report["missing_project"] += 1
                continue
            store = DocumentHistoryStore(project_dir)
            existing = store.get_unit(store._unit_id(request["request_id"], "request"))
            if existing and not (check or repair):
                report["existing"] += 1
                continue
            global_events, documents, revisions, truncated = _extract(request, project_dir)
            report["document_units"] += len(documents)
            report["truncated_events"] += truncated
            report["revision_references"] += sum(len(items) for items in revisions.values())
            verified = {}
            for path, items in revisions.items():
                file = project_dir / path
                if not file.is_file():
                    continue
                rendered = file.read_text(encoding="utf-8")
                info = revision_manager.parse(rendered)
                if (not info.legacy and info.revision_id in {str(item["revision_id"]) for item in items}
                        and info.metadata.get("body_sha256") == hashlib.sha256(info.body.encode("utf-8")).hexdigest()):
                    verified[path] = (info, items)
            report["verified_current_revisions"] += len(verified)
            report["unverified_revision_references"] += sum(
                len(items) - int(path in verified) for path, items in revisions.items()
            )
            if check:
                if existing:
                    report["checked"] += 1
                    for problem in _check_saved(store, request, global_events, documents, verified):
                        report["errors"].append({"request_id": request["request_id"], "error": problem})
                continue
            if repair:
                if existing and existing["analysis_status"] == "deferred" and existing["trace_ids"] == request["trace_ids"]:
                    report["repaired_events"] += _append_missing(
                        store, request["request_id"], global_events, documents,
                    )
                    for problem in _check_saved(store, request, global_events, documents, verified):
                        report["errors"].append({"request_id": request["request_id"], "error": problem})
                continue
            if not apply:
                continue
            try:
                store.start_request(request["request_id"], request["session_id"], request["user_input"])
                for kind, content in global_events:
                    store.append_request_event(request["request_id"], kind, content)
                for path, entries in documents.items():
                    for kind, content in entries:
                        store.append_document_event(request["request_id"], path, kind, content)
                for path, (info, items) in verified.items():
                    current = store.latest(path)
                    if current is None:
                        store.ensure_baseline(project_dir / path)
                    elif current["revision_id"] != info.revision_id:
                        raise ValueError(f"History head differs from disk: {path}")
                    if str(items[-1]["revision_id"]) == info.revision_id:
                        store.attach_revision(request["request_id"], path, info.revision_id,
                                              str(info.metadata.get("parent_revision_id") or ""))
                unit_ids = store.close_request(request["request_id"], request["status"],
                                               request["answer"], request["trace_ids"], request["turn_no"])
                with store._connect() as history_db:
                    history_db.execute("UPDATE history_units SET created_at = ? WHERE request_id = ?",
                                       (request["started_at"], request["request_id"]))
                for history_id in [*unit_ids, store._unit_id(request["request_id"], "request")]:
                    store.set_analysis_status(history_id, "deferred")
                problems = _check_saved(store, request, global_events, documents, verified)
                if problems:
                    raise ValueError("; ".join(problems))
                report["imported"] += 1
            except (OSError, ValueError, sqlite3.Error) as exc:
                report["errors"].append({"request_id": request["request_id"], "error": str(exc)})
    finally:
        db.close()
    return report


def rebuild_project_history(trace_db: Path, workspace: Path, project_id: str,
                            *, apply: bool = False) -> dict:
    """Replace a project's deferred Trace imports, restoring the DB on any failure."""
    workspace = Path(workspace).resolve()
    project_dir = (workspace / project_id).resolve()
    if project_dir.parent != workspace or not project_dir.is_dir():
        raise ValueError("--project-id must identify one project directory")
    store = DocumentHistoryStore(project_dir)
    if not store.db_path.is_file():
        raise FileNotFoundError(store.db_path)
    with store._connect() as history_db:
        counts = dict(history_db.execute(
            "SELECT kind, COUNT(*) FROM history_units GROUP BY kind"))
        unsafe = history_db.execute("""SELECT COUNT(*) FROM history_units
            WHERE analysis_status NOT IN ('deferred', 'routed') OR status = 'collecting'""").fetchone()[0]
        authored = history_db.execute(
            "SELECT COUNT(*) FROM revisions WHERE actor != 'baseline'").fetchone()[0]
    if unsafe or authored:
        raise ValueError("History contains live or analyzed records; refusing to replace them")
    report = {"mode": "rebuild" if apply else "rebuild_dry_run",
              "project_id": project_id, "old_units": counts, "errors": []}
    if not apply:
        return report
    backup = store.db_path.with_suffix(".pre-rebuild.bak")
    if backup.exists():
        raise FileExistsError(f"Existing rebuild backup must be reviewed first: {backup}")
    with closing(sqlite3.connect(store.db_path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    try:
        with store._connect() as history_db:
            history_db.execute("DELETE FROM history_events")
            history_db.execute("DELETE FROM history_units")
        imported = backfill(trace_db, workspace, apply=True, project_id=project_id)
        if imported["errors"]:
            raise ValueError(f"Trace import failed: {imported['errors']}")
        reorganized = reorganize(trace_db, workspace, apply=True, project_id=project_id)
        if reorganized["errors"]:
            raise ValueError(f"Run grouping failed: {reorganized['errors']}")
        with store._connect() as history_db:
            document_count = history_db.execute(
                "SELECT COUNT(*) FROM history_units WHERE kind = 'document'").fetchone()[0]
        if document_count != reorganized["run_units"]:
            raise ValueError("Run grouping left aggregated or missing document History")
        checked = backfill(trace_db, workspace, check=True, project_id=project_id)
        if checked["errors"]:
            raise ValueError(f"History verification failed: {checked['errors']}")
        report.update({"imported": imported, "reorganized": reorganized,
                       "checked": checked["checked"]})
    except Exception as exc:
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(store.db_path)) as destination:
            source.backup(destination)
        report["errors"].append(str(exc))
        return report
    backup.unlink()
    return report
