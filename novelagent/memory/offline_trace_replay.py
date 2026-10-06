"""Replay a completed retain Trace into a disposable knowledge snapshot."""

import copy
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from novelagent.history import DocumentHistoryStore
from novelagent.history.sources import root_history_ids
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.offline_migration import _history, _read_db, offline_snapshot, source_manifest
from novelagent.memory.models import retrieval_text


class ReplayRejected(ValueError):
    def __init__(self, code, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _trace_bundle(database, project_id, trace_id):
    with _read_db(Path(database)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"traces", "trace_events"} <= tables:
            raise ReplayRejected("trace_schema_missing")
        trace_row = db.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
        if not trace_row:
            raise ReplayRejected("trace_not_found")
        trace = dict(trace_row)
        if trace.get("project_id") != project_id:
            raise ReplayRejected("trace_project_mismatch")
        if trace.get("status") != "completed":
            raise ReplayRejected("trace_not_completed", {"status": trace.get("status", "")})
        events = [dict(row) for row in db.execute(
            "SELECT * FROM trace_events WHERE trace_id=? ORDER BY sequence_no", (trace_id,))]

    parsed = [(event, json.loads(event["payload_json"])) for event in events]
    actors = [payload.get("actor") for event, payload in parsed if event["event_type"] == "agent_started"]
    if actors != ["knowledge:retain"]:
        raise ReplayRejected("trace_not_retain")
    requests = [payload for event, payload in parsed if event["event_type"] == "llm_request"]
    if len(requests) != 1:
        raise ReplayRejected("retain_request_missing_or_ambiguous")
    pieces = [payload for event, payload in parsed if event["event_type"] == "agent_request_message"]
    message_count = requests[0].get("message_count")
    if not isinstance(message_count, int) or message_count < 1:
        raise ReplayRejected("request_message_count_invalid")
    grouped = {index: [] for index in range(message_count)}
    for piece in pieces:
        index = piece.get("message_index")
        chunk_index = piece.get("chunk_index")
        chunk_count = piece.get("chunk_count")
        if (not isinstance(index, int) or index not in grouped or not isinstance(chunk_index, int)
                or not isinstance(chunk_count, int) or chunk_count < 1 or not isinstance(piece.get("content"), str)):
            raise ReplayRejected("request_message_chunk_invalid")
        grouped[index].append(piece)
    messages = []
    for index in range(message_count):
        chunks = sorted(grouped[index], key=lambda part: part["chunk_index"])
        if (not chunks or [part["chunk_index"] for part in chunks] != list(range(len(chunks)))
                or any(part["chunk_count"] != len(chunks) for part in chunks)):
            raise ReplayRejected("request_message_chunk_incomplete", {"message_index": index})
        metadata = {key: value for key, value in chunks[0].items()
                    if key not in {"message_index", "chunk_index", "chunk_count", "content"}}
        if any({key: value for key, value in chunk.items()
                if key not in {"message_index", "chunk_index", "chunk_count", "content"}} != metadata for chunk in chunks):
            raise ReplayRejected("request_message_metadata_conflict", {"message_index": index})
        messages.append({**metadata, "content": "".join(chunk["content"] for chunk in chunks)})

    final_answer = trace.get("final_answer") or ""
    try:
        response = json.loads(final_answer)
    except (TypeError, ValueError):
        raise ReplayRejected("retain_final_answer_invalid_json") from None
    if not isinstance(response, dict) or not isinstance(response.get("memories"), list) or not response["memories"]:
        raise ReplayRejected("retain_final_answer_missing_memories")
    route_events = [payload for event, payload in parsed if event["event_type"] == "model_route"]
    usage_events = [payload for event, payload in parsed if event["event_type"] == "llm_usage"]
    digest = _json_hash({"trace": trace, "events": events})
    return {
        "trace": trace,
        "events": events,
        "messages": messages,
        "request_text": "\n".join(str(message.get("content", "")) for message in messages),
        "response": response,
        "trace_record_hash": digest,
        "has_model_route_request_id": any(bool(row.get("request_id")) for row in route_events),
        "has_usage": bool(usage_events),
    }


def _package_source_ids(package):
    return list(dict.fromkeys([
        *(event_id for fact in package.get("facts", []) for event_id in fact.get("source_event_ids", [])),
        *(event_id for relation in package.get("relations", []) for event_id in relation.get("source_event_ids", [])),
        *(event_id for change in package.get("fact_changes", []) for event_id in change.get("source_event_ids", [])),
        *(fragment["source_event_id"] for fragment in package.get("fragments", []) if fragment.get("source_event_id")),
    ]))


def _map_history_source_ids(project, packages, request_text):
    units, events, aliases, _ = _history(project)
    raw_ids = list(dict.fromkeys(event_id for package in packages for event_id in _package_source_ids(package)))
    if not raw_ids:
        raise ReplayRejected("retain_has_no_source_event_ids")
    mapped, owners = {}, set()
    for source_id in raw_ids:
        if source_id not in request_text:
            raise ReplayRejected("request_does_not_cover_source", {"source_event_id": source_id})
        matches = [source_id] if source_id in events else aliases.get(source_id, [])
        if len(matches) != 1:
            raise ReplayRejected("history_source_missing_or_ambiguous", {"source_event_id": source_id})
        event_id = matches[0]
        mapped[source_id] = event_id
        owners.add(events[event_id]["history_id"])
    roots = root_history_ids(project, sorted(owners))
    if len(roots) != 1:
        raise ReplayRejected("history_mapping_not_unique", {"history_count": len(roots)})
    return roots[0], mapped


def _remap_package_sources(package, mapping):
    package = copy.deepcopy(package)
    for fact in package.get("facts", []):
        fact["source_event_ids"] = [mapping.get(event_id, event_id) for event_id in fact.get("source_event_ids", [])]
    for relation in package.get("relations", []):
        relation["source_event_ids"] = [mapping.get(event_id, event_id) for event_id in relation.get("source_event_ids", [])]
    for change in package.get("fact_changes", []):
        change["source_event_ids"] = [mapping.get(event_id, event_id) for event_id in change.get("source_event_ids", [])]
    for fragment in package.get("fragments", []):
        if fragment.get("source_event_id"):
            fragment["source_event_id"] = mapping.get(fragment["source_event_id"], fragment["source_event_id"])
    return package


def _fixture_vector(payload):
    """Deterministic, explicitly non-production vector for isolated persistence checks."""
    digest = hashlib.sha256(retrieval_text("memory", payload).encode("utf-8")).digest()
    vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) - 127.5
    return vector, "offline-retain-replay-sha256-fixture"


def replay_completed_retain_trace(project, trace_database, trace_id):
    """Validate a retained Memory response and save it only in a disposable snapshot."""
    project = Path(project).resolve()
    trace_database = Path(trace_database).resolve()
    if not project.is_dir() or not trace_database.is_file():
        raise ReplayRejected("input_path_missing")
    before = source_manifest(project)
    trace = _trace_bundle(trace_database, project.name, trace_id)
    history_id, source_mapping = _map_history_source_ids(project, trace["response"]["memories"], trace["request_text"])
    result = {
        "mode": "offline_completed_retain_replay",
        "status": "rejected",
        "project_id": project.name,
        "trace_id": trace_id,
        "trace_status": trace["trace"]["status"],
        "history_id": history_id,
        "request_message_count": len(trace["messages"]),
        "request_messages_reconstructed": True,
        "request_contains_cited_source_ids": True,
        "retain_package_count": len(trace["response"]["memories"]),
        "trace_final_answer_sha256": hashlib.sha256(trace["trace"]["final_answer"].encode("utf-8")).hexdigest(),
        "trace_record_hash_before": trace["trace_record_hash"],
        "trace_record_hash_after": "",
        "trace_record_unchanged": False,
        "has_model_route_request_id": trace["has_model_route_request_id"],
        "has_usage": trace["has_usage"],
        "source_hashes_unchanged": False,
        "snapshot_verified": False,
        "application_llm_calls": 0,
        "embedding_calls": 0,
        "insight_saved": False,
        "packages": [],
    }
    with tempfile.TemporaryDirectory(prefix="novelagent-retain-replay-") as temp:
        snapshot = Path(temp) / project.name
        snapshot_report = offline_snapshot(project, snapshot, trace_database)
        if not snapshot_report["snapshot_verified"] or not snapshot_report["source_unchanged"]:
            raise ReplayRejected("snapshot_verification_failed")
        history = DocumentHistoryStore(snapshot)
        unit = history.get_unit(history_id)
        if not unit:
            raise ReplayRejected("mapped_history_missing_from_snapshot")
        pipeline = object.__new__(KnowledgePipeline)
        pipeline.workspace_dir = Path(temp)
        evidence, _ = pipeline._evidence(project.name, unit)
        evidence_by_id = {event["event_id"]: event for event in evidence}
        prepared, entries, package_reports = [], [], []
        for index, raw in enumerate(trace["response"]["memories"], 1):
            package = _remap_package_sources(raw, source_mapping)
            try:
                if any("revision_id" not in f for f in package.get("fragments", [])):
                    package = pipeline._model_package(project.name, package, evidence)
                else:
                    package = pipeline._validate_package(package, evidence)
            except ValidationError as exc:
                errors = [{"loc": list(error["loc"]), "type": error["type"]}
                          for error in exc.errors(include_input=False)]
                raise ReplayRejected("memory_schema_invalid", {"package_index": index, "errors": errors}) from None
            except (ValueError, TypeError, KeyError):
                raise ReplayRejected("memory_package_evidence_invalid", {"package_index": index}) from None
            allowed_paths = pipeline._allowed_paths(unit, evidence, [])
            if set(package["artifact_paths"]) - allowed_paths:
                raise ReplayRejected("memory_artifact_scope_invalid", {"package_index": index})
            try:
                pipeline._validate_fragments(project.name, package, allowed_paths, evidence)
            except (ValueError, TypeError, KeyError):
                raise ReplayRejected("memory_fragment_unverified", {"package_index": index}) from None
            if unit.get("status") != "completed":
                package["completion"] = "partial"
            cited = set(_package_source_ids(package))
            if cited - evidence_by_id.keys():
                raise ReplayRejected("history_evidence_missing", {"package_index": index})
            selected = [evidence_by_id[eid] for eid in sorted(cited)]
            if package["source_text"]:
                selected.extend(event for event in evidence if event["kind"] in {"user_input", "user_answer"}
                                and pipeline._quote_matches(package["source_text"], event))
            selected = list({event["event_id"]: event for event in selected}.values())
            vector, model_id = _fixture_vector(package)
            prepared.append(package)
            entries.append({"payload": package, "evidence": selected, "vector": vector, "model_id": model_id,
                            "operation_id": f"offline-retain-replay:{trace_id}:{index}", "batch_id": f"new:{index}"})
            package_reports.append({"index": index, "schema_valid": True,
                "source_event_ids": sorted(cited), "source_event_mapping": [
                    {"trace_output_id": old, "history_event_id": new}
                    for old, new in sorted(source_mapping.items()) if old in _package_source_ids(raw)],
                "fact_count": len(package["facts"]),
                "payload_sha256": _json_hash(package)})
        store = KnowledgeStore(snapshot)
        saved = store.save_memories(entries)
        for index, (record, package, entry) in enumerate(zip(saved, prepared, entries), 1):
            loaded = store.get(record["id"], record["version"])
            if (not loaded or loaded["layer"] != "memory"
                    or loaded["payload"]["title"] != package["title"]
                    or loaded["payload"]["direction"] != package["direction"]
                    or [(f["claim"], f["domain"], f["source_event_ids"], f["status"]) for f in loaded["payload"]["facts"]]
                    != [(f["claim"], f["domain"], f["source_event_ids"], f["status"]) for f in package["facts"]]):
                raise ReplayRejected("memory_readback_mismatch", {"package_index": index})
            if {e["event_id"] for e in loaded["evidence"]} != {e["event_id"] for e in entry["evidence"]}:
                raise ReplayRejected("memory_evidence_readback_mismatch", {"package_index": index})
            package_reports[index - 1].update(memory_id=record["id"], version=record["version"], readback_verified=True)
        if source_manifest(project) != before:
            raise ReplayRejected("source_project_changed")
        after_trace = _trace_bundle(trace_database, project.name, trace_id)
        result.update(status="replayed_in_temporary_snapshot", snapshot_verified=True,
                      source_hashes_unchanged=True, trace_record_hash_after=after_trace["trace_record_hash"],
                      trace_record_unchanged=after_trace["trace_record_hash"] == trace["trace_record_hash"],
                      packages=package_reports)
        if not result["trace_record_unchanged"]:
            raise ReplayRejected("source_trace_changed")
    result["temporary_snapshot_removed"] = True
    result["vector"] = "local SHA-256 fixture; not suitable for production retrieval"
    result["insight_note"] = "No Insight was generated or saved; consolidation requires a separate model result."
    return result
