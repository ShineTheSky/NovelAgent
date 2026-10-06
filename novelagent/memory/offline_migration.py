"""Offline provenance audit and isolated snapshots; never infer new knowledge."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import numpy as np
import yaml
from pydantic import ValidationError

from novelagent.memory.models import Insight, MemoryPackage


def _read_db(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


def _history(project):
    path = project / ".history/history.sqlite3"
    if not path.exists():
        return {}, {}, {}, {}
    with _read_db(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        units = {r["history_id"]: dict(r) for r in db.execute("SELECT * FROM history_units")}
        events = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM history_events")}
        aliases = {}
        if "history_source_events" in tables:
            for row in db.execute("SELECT source_event_id,event_id FROM history_source_events"):
                aliases.setdefault(row[0], []).append(row[1])
        revisions = {r["revision_id"]: dict(r) for r in db.execute("""SELECT r.*,d.path FROM revisions r
            JOIN documents d ON d.document_id=r.document_id""")} if "revisions" in tables else {}
    return units, events, aliases, revisions


def _legacy(project):
    for layer in ("memory", "insight", "pattern"):
        for path in sorted((project / ("." + layer)).rglob("*.md")):
            relative = path.relative_to(project)
            if "archive" in relative.parts or path.name in {"memory.md", "memory_archive.md", "insight.md", "pattern.md"}:
                continue
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) == 3:
                    header = yaml.safe_load(parts[1]) or {}
                    if isinstance(header, dict) and header.get("id"):
                        yield str(relative).replace("\\", "/"), layer, header, parts[2].strip()
                        continue
            # Old project_rules.md stores independently identified sections, not frontmatter.
            sections = list(re.finditer(r"^## \[(rule|correction|decision|context)\] (.+)\n<!-- memory-entry\n(.*?)\n-->", text, re.M | re.S))
            for index, section in enumerate(sections):
                header = yaml.safe_load(section[3]) or {}
                if isinstance(header, dict) and header.get("id"):
                    end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
                    header.setdefault("title", section[2])
                    yield str(relative).replace("\\", "/") + "#" + str(header["id"]), layer, header, text[section.end():end].strip()


def _trace_views(source):
    payload = json.loads(source["payload_json"])
    kind = source["event_type"]
    if kind == "user_message":
        return [("user_input", str(payload.get("content") or ""))]
    if kind == "user_answer":
        return [("user_answer", json.dumps(payload.get("answers", payload), ensure_ascii=False))]
    if kind == "assistant_turn":
        return [("main_agent_output", str(payload.get("content") or "")),
                ("main_agent_thinking", str(payload.get("reasoning_content") or ""))]
    if kind in {"tool_call", "subagent_tool_call"} and payload.get("tool") == "SubAgent":
        return [("main_delegation", str((payload.get("params") or {}).get("task") or ""))]
    if kind in {"subagent_subagent_done", "workflow_done"}:
        return [(str(payload.get("preset") or "") + "_output", str(payload.get("result") or ""))]
    return []


def _trace_sources(project, database, ids):
    if not database or not ids:
        return {}
    result = {}
    with _read_db(Path(database)) as db:
        ids = sorted(ids)
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            for row in db.execute(f"""SELECT e.* FROM trace_events e JOIN traces t ON t.trace_id=e.trace_id
                WHERE e.event_id IN ({marks}) AND t.project_id=?""", [*batch, project.name]):
                result[row["event_id"]] = dict(row)
    return result


def _legacy_plan(project, units, events, aliases, trace_database=None):
    result = []
    for path, layer, header, body in _legacy(project):
        semantic = [e for e in header.get("semantic_evidence", []) if isinstance(e, dict)]
        history_ids = list(dict.fromkeys([*header.get("history_ids", []),
                                         *(e["history_id"] for e in semantic if e.get("history_id"))]))
        ids = list(dict.fromkeys([*header.get("history_event_ids", []), *header.get("source_event_ids", []),
                                 *(eid for e in semantic for eid in e.get("history_event_ids", []))]))
        resolved, missing = {}, []
        for eid in ids:
            matches = [eid] if eid in events else aliases.get(eid, [])
            if not matches:
                missing.append(eid)
            for match in matches:
                resolved[match] = events[match]
        mismatches = []
        for entry in semantic:
            for excerpt in entry.get("history_excerpts", []):
                event = events.get(excerpt.get("event_id"))
                if not event or not event["content"].startswith(str(excerpt.get("content") or "")):
                    mismatches.append(excerpt.get("event_id"))
        missing_histories = [hid for hid in history_ids if hid not in units]
        ready = bool(resolved) and not (missing or mismatches or missing_histories)
        result.append({"legacy_id": str(header["id"]), "path": path, "source_layer": layer,
                       "status": "evidence_staged" if ready else "review_required",
                       "history_ids": list(dict.fromkeys([*history_ids, *(e["history_id"] for e in resolved.values())])),
                       "missing_history_ids": missing_histories, "missing_event_ids": missing,
                       "mismatched_excerpt_ids": mismatches,
                       "payload": {"legacy_metadata": header, "legacy_body": body,
                                   "semantic_status": "unverified_legacy_summary", "learning_status": "not_started"},
                       "evidence": list(resolved.values()),
                       "reason": "Exact source events can be staged; direction/facts/learning still require semantic review."
                           if ready else "Missing or mismatched evidence; do not activate or rename old layers."})
    sources = _trace_sources(project, trace_database, {eid for row in result for eid in row["missing_event_ids"]})
    for row in result:
        raw, mapped = [], []
        for eid in list(row["missing_event_ids"]):
            source = sources.get(eid)
            if not source:
                continue
            raw.append(source)
            candidates = []
            for kind, content in _trace_views(source):
                if not content:
                    continue
                for event in events.values():
                    owner = units[event["history_id"]]
                    root = units.get(owner.get("requirement_history_id")) or owner
                    traces = {*json.loads(owner.get("trace_ids_json") or "[]"), *json.loads(root.get("trace_ids_json") or "[]")}
                    if event["kind"] == kind and event["content"] == content and source["trace_id"] in traces:
                        candidates.append(event)
            if candidates:
                mapped.append(eid)
                known = {e["event_id"] for e in row["evidence"]}
                row["evidence"].extend(e for e in candidates if e["event_id"] not in known)
                row["missing_event_ids"].remove(eid)
        row["trace_evidence"] = raw
        row["exact_trace_history_mappings"] = mapped
        row["trace_unmapped_types"] = dict(Counter(source["event_type"] for source in raw
                                                   if source["event_id"] in row["missing_event_ids"]))
        if raw:
            if not (row["missing_event_ids"] or row["missing_history_ids"] or row["mismatched_excerpt_ids"]):
                row.update(status="evidence_staged", reason="Trace ID plus exact event content maps to History; semantics remain unverified.")
            elif all(eid in sources for eid in row["missing_event_ids"]) and not (row["missing_history_ids"] or row["mismatched_excerpt_ids"]):
                row.update(status="trace_rebuild_required", reason="Original project Trace events exist, but History event mapping must be rebuilt; no semantic inference performed.")
            if not row["evidence"] and all(source["event_type"] == "error" for source in raw):
                row.update(status="badcase_only", reason="Only execution failure evidence exists; keep it in BadCase, not Memory learning.")
    return result


def _quote_matches(quote, content):
    if quote in content:
        return True
    try:
        value = json.loads(content)
    except ValueError:
        return False
    def strings(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        return []
    return any(quote in text for text in strings(value))


def _structured_plan(project, events, revisions):
    path = project / ".knowledge/knowledge.sqlite3"
    if not path.exists():
        return [], []
    with _read_db(path) as db:
        rows = [dict(r) for r in db.execute("""SELECT r.layer,r.status,r.current_version,r.published_version,v.*
            FROM records r JOIN versions v ON v.id=r.id ORDER BY v.id,v.version""")]
        integrity = [str(r[0]) for r in db.execute("PRAGMA integrity_check") if r[0] != "ok"]
        integrity += ["foreign_key_violation:" + str(tuple(r)) for r in db.execute("PRAGMA foreign_key_check")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lineage = [dict(r) for r in db.execute("SELECT * FROM fact_changes")] if "fact_changes" in tables else []
    versions = {(r["id"], r["version"]): r for r in rows}
    result = []
    for row in rows:
        issues = []
        payload = {}
        try:
            payload, evidence = json.loads(row["payload"]), json.loads(row["evidence"])
            (MemoryPackage if row["layer"] == "memory" else Insight).model_validate(payload)
            evidence_ids = {e["event_id"] for e in evidence}
            if not evidence:
                issues.append("missing_evidence")
            for entry in evidence:
                event = events.get(entry["event_id"])
                if not event or any(str(entry.get(k, "")) != str(event.get(k, ""))
                                    for k in ("content", "kind", "history_id", "revision_id")):
                    issues.append("unverified_event:" + entry["event_id"])
            for fact in payload.get("facts", []):
                if set(fact["source_event_ids"]) - evidence_ids:
                    issues.append("unavailable_fact_evidence:" + fact["fact_id"])
            previous = versions.get((row["id"], row["previous_version"]))
            if row["previous_version"] != row["version"] - 1 or (row["previous_version"] and not previous):
                issues.append("broken_version_chain")
            previous_facts = {f["fact_id"]: f for f in json.loads(previous["payload"]).get("facts", [])} if previous else {}
            current_facts = {f["fact_id"]: f for f in payload.get("facts", [])}
            for change in payload.get("fact_changes", []):
                if set(change["previous_fact_ids"]) - previous_facts.keys():
                    issues.append("unavailable_previous_fact_change")
                if set(change["current_fact_ids"]) - current_facts.keys():
                    issues.append("unavailable_current_fact_change")
                if set(change["source_event_ids"]) - evidence_ids:
                    issues.append("unavailable_fact_change_evidence")
                if change["kind"] == "withdrawn" and any(current_facts.get(fid, {}).get("status", "active") != "withdrawn"
                                                        for fid in change["current_fact_ids"]):
                    issues.append("withdrawal_fact_still_active")
            for change in lineage:
                if (change["id"], change["version"]) != (row["id"], row["version"]):
                    continue
                if change["previous_version"] != row["previous_version"] or change["previous_fact_id"] not in previous_facts:
                    issues.append("invalid_lineage_previous_endpoint")
                if change["current_fact_id"] and change["current_fact_id"] not in current_facts:
                    issues.append("invalid_lineage_current_endpoint")
                if set(json.loads(change["source_event_ids"])) - evidence_ids:
                    issues.append("invalid_lineage_evidence")
            owner_fact_ids = set(current_facts)
            for source in payload.get("sources", []):
                saved = versions.get((source["memory_id"], source["version"]))
                if saved:
                    owner_fact_ids.update(f["fact_id"] for f in json.loads(saved["payload"]).get("facts", [])
                                          if not source["fact_ids"] or f["fact_id"] in source["fact_ids"])
            for relation in payload.get("relations", []):
                if set(relation["source_event_ids"]) - evidence_ids:
                    issues.append("unavailable_relation_evidence")
                if relation["source_fact_id"] and relation["source_fact_id"] not in owner_fact_ids:
                    issues.append("unavailable_relation_source_fact")
                target = versions.get((relation["target_id"], relation["target_version"]))
                if not target:
                    issues.append("unavailable_relation_target")
                elif relation.get("target_fact_id") not in {"", *(f["fact_id"] for f in json.loads(target["payload"]).get("facts", []))}:
                    issues.append("unavailable_relation_fact")
            for source in payload.get("sources", []):
                target = versions.get((source["memory_id"], source["version"]))
                if not target or target["layer"] != "memory":
                    issues.append("unavailable_source_memory")
                elif set(source["fact_ids"]) - {f["fact_id"] for f in json.loads(target["payload"]).get("facts", [])}:
                    issues.append("unavailable_source_fact")
                elif source["role"] == "support":
                    target_facts = json.loads(target["payload"])["facts"]
                    selected = [f for f in target_facts if not source["fact_ids"] or f["fact_id"] in source["fact_ids"]]
                    if not any(f.get("status", "active") == "active" for f in selected) or (
                        source["fact_ids"] and any(f.get("status", "active") != "active" for f in selected)
                    ):
                        issues.append("withdrawn_fact_support")
            if row["layer"] != "memory" and not any(s["role"] == "support" for s in payload["sources"]):
                issues.append("missing_support")
            for fragment in payload.get("fragments", []):
                revision = revisions.get(fragment["revision_id"])
                if revision and (revision.get("parent_revision_id") or "") != fragment["previous_revision_id"]:
                    issues.append("artifact_parent_revision_mismatch")
                for revision_key, quote_key in (("revision_id", "after"), ("previous_revision_id", "before")):
                    if fragment[revision_key]:
                        revision = revisions.get(fragment[revision_key])
                        if not revision or revision["path"] != fragment["path"] or fragment[quote_key] not in revision["body_md"]:
                            issues.append("unverified_artifact_fragment")
            quote = payload.get("source_text")
            if quote and not any(e["kind"] in {"user_input", "user_answer"} and _quote_matches(quote, e["content"]) for e in evidence):
                issues.append("unverified_user_quote")
            if (payload.get("authority") == "human" or payload.get("human_confirmed") or any(s.get("human_selected") for s in payload.get("solutions", []))) and not any(
                e["kind"] in {"user_input", "user_answer"} for e in evidence
            ):
                issues.append("unverified_human_authority")
            vector = np.frombuffer(row["vector"], dtype=np.float32)
            if len(vector) != row["dimensions"] or not len(vector) or not np.isfinite(vector).all() or not np.linalg.norm(vector):
                issues.append("invalid_vector")
        except ValidationError as exc:
            issues.extend("invalid_structure:" + ".".join(str(v) for v in error["loc"]) + ":" + error["type"]
                          for error in exc.errors(include_input=False))
        except (ValueError, TypeError, KeyError) as exc:
            issues.append("invalid_structure:" + type(exc).__name__)
        result.append({"id": row["id"], "version": row["version"], "layer": row["layer"],
                       "published": row["version"] == row["published_version"], "status": row["status"],
                       "issues": list(dict.fromkeys(issues)), "sources": payload.get("sources", []) if isinstance(payload, dict) else []})
    # A structurally correct Insight is not usable if any pinned source failed verification.
    bad = {(r["id"], r["version"]) for r in result if r["issues"]}
    for row in result:
        if any((s["memory_id"], s["version"]) in bad for s in row["sources"]):
            row["issues"].append("source_memory_requires_review")
    return result, integrity


def offline_plan(project, trace_database=None):
    """Read-only plan, including exact content verification, with no store/client creation."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("Project directory does not exist")
    units, events, aliases, revisions = _history(project)
    legacy = _legacy_plan(project, units, events, aliases, trace_database)
    structured, integrity = _structured_plan(project, events, revisions)
    published = [r for r in structured if r["published"]]
    return {"project": str(project), "mode": "offline_no_models", "legacy": legacy,
            "structured": structured, "integrity_issues": integrity,
            "counts": {"legacy_layers": dict(Counter(r["source_layer"] for r in legacy)),
                       "legacy_statuses": dict(Counter(r["status"] for r in legacy)),
                       "structured_layers": dict(Counter(r["layer"] for r in published)),
                       "structured_versions": len(structured),
                       "verified_published": sum(not r["issues"] for r in published),
                       "review_published": sum(bool(r["issues"]) for r in published)},
            "note": "Verified structured records can be copied without inference. Legacy summaries stay evidence bundles, not new Memory/Insight/Pattern."}


def _backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _read_db(source) as db, closing(sqlite3.connect(destination)) as target:
        db.backup(target)


def source_manifest(project):
    """Hash only migration inputs, without including source text in the report."""
    project = Path(project).resolve()
    paths = {project / relative for relative in (".history/history.sqlite3", ".knowledge/knowledge.sqlite3")}
    paths.update(project / row[0].split("#", 1)[0] for row in _legacy(project))
    result = {}
    for path in sorted(paths):
        if path.exists():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[str(path.relative_to(project))] = digest.hexdigest()
    return result


def offline_snapshot(project, destination, trace_database=None, *, repair_history=False):
    """Explicit empty isolated destination only; production is never edited."""
    project, destination = Path(project).resolve(), Path(destination).resolve()
    if destination == project or destination in project.parents or project in destination.parents:
        raise ValueError("Destination must be outside the source project")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be an empty isolated directory")
    if repair_history and not trace_database:
        raise ValueError("History repair requires the original read-only Trace database")
    before = source_manifest(project)
    report = offline_plan(project, trace_database)
    if report["integrity_issues"]:
        raise ValueError("Source knowledge database failed integrity checks")
    destination.mkdir(parents=True, exist_ok=True)
    for relative in (".history/history.sqlite3", ".knowledge/knowledge.sqlite3"):
        source = project / relative
        if source.exists():
            _backup(source, destination / relative)
    if (destination / ".knowledge/knowledge.sqlite3").exists():
        with closing(sqlite3.connect(destination / ".knowledge/knowledge.sqlite3")) as db:
            for row in report["structured"]:
                if row["published"] and row["issues"]:
                    db.execute("UPDATE records SET status='migration_review' WHERE id=?", (row["id"],))
            # A snapshot is a read/retrieval test fixture, never authorization to recover jobs.
            db.execute("UPDATE jobs SET status='migration_hold' WHERE status IN ('pending','failed')")
            db.commit()
    with closing(sqlite3.connect(destination / "migration_evidence.sqlite3")) as db:
        db.execute("""CREATE TABLE bundles (path TEXT PRIMARY KEY, legacy_id TEXT NOT NULL,
            source_layer TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, evidence TEXT NOT NULL)""")
        for row in report["legacy"]:
            db.execute("INSERT INTO bundles VALUES (?,?,?,?,?,?)", (row["path"], row["legacy_id"], row["source_layer"],
                row["status"], json.dumps(row["payload"], ensure_ascii=False),
                json.dumps({"history": row["evidence"], "trace": row.get("trace_evidence", [])}, ensure_ascii=False)))
        db.commit()
    if repair_history:
        from novelagent.memory.offline_history_repair import repair_snapshot_sources
        report["history_repair"] = repair_snapshot_sources(destination, trace_database, project.name, report["legacy"])
        units, events, aliases, _ = _history(destination)
        refreshed = _legacy_plan(project, units, events, aliases, trace_database)
        previous = {row["path"]: row for row in report["legacy"]}
        for row in refreshed:
            row["trace_evidence"] = list({source["event_id"]: source for source in
                [*previous[row["path"]].get("trace_evidence", []), *row.get("trace_evidence", [])]}.values())
        report["legacy"] = refreshed
        report["counts"]["legacy_statuses"] = dict(Counter(row["status"] for row in refreshed))
        with closing(sqlite3.connect(destination / "migration_evidence.sqlite3")) as db:
            for row in refreshed:
                db.execute("UPDATE bundles SET status=?,evidence=? WHERE path=?", (row["status"],
                    json.dumps({"history": row["evidence"], "trace": row["trace_evidence"]}, ensure_ascii=False), row["path"]))
            db.commit()
    copied = offline_plan(destination)
    if copied["structured"] != report["structured"]:
        # Quarantining is the only permitted change to published metadata.
        quarantined = {r["id"] for r in report["structured"] if r["published"] and r["issues"]}
        expected = [{**r, "status": "migration_review" if r["id"] in quarantined else r["status"]}
                    for r in report["structured"]]
        if copied["structured"] != expected:
            raise ValueError("Snapshot verification differs from the source")
    if source_manifest(project) != before:
        raise ValueError("Source changed during migration; discard this snapshot and re-audit")
    return {**report, "destination": str(destination), "snapshot_verified": True,
            "source_unchanged": True, "learning_jobs_held": True}


async def verify_snapshot_runtime(destination):
    """Exercise retrieval/context/scoring in a snapshot with saved vectors, not a model.

    This verifies wiring and provenance, never semantic retrieval quality.
    """
    from types import SimpleNamespace
    from novelagent.memory.knowledge_store import KnowledgeStore
    from novelagent.memory.retrieval import KnowledgeRetrieval
    from novelagent.memory.runtime import KnowledgeRuntime

    destination = Path(destination).resolve()
    if not (destination / "migration_evidence.sqlite3").exists():
        raise ValueError("Runtime verification requires an isolated migration snapshot")
    store = KnowledgeStore(destination)
    records = store.list(("insight",))
    result = {"query_embedding": "existing_saved_vector_fixture_not_semantic_evaluation",
              "memories_checked": 0, "memory_title_window_checked": 0, "memory_recall_checked": 0,
              "insights_checked": 0, "source_memory_versions_checked": 0, "history_events_checked": 0,
              "context_attachments_checked": 0, "score_and_dedup_checked": 0, "pattern_default_checked": 0,
              "errors": []}
    units, events, _, _ = _history(destination)
    for memory in store.list(("memory",)):
        vector = np.frombuffer(memory["vector"], dtype=np.float32).copy()
        class SavedMemoryVector:
            async def encode(self, texts):
                return np.stack([vector for _ in texts])
        try:
            pipeline = SimpleNamespace(store=lambda _: store)
            if memory["id"] + "@" + str(memory["version"]) not in KnowledgeRuntime(pipeline).index("snapshot"):
                raise ValueError("Memory is missing from the title window")
            result["memory_title_window_checked"] += 1
            recall = await KnowledgeRetrieval(store, SavedMemoryVector(), memory["model_id"]).recall(
                memory["payload"]["direction"], purpose="memory_evidence", limit=100)
            if not any(item["record"]["id"] == memory["id"] for item in recall["items"]):
                raise ValueError("Saved Memory vector could not retrieve its evidence package")
            result["memory_recall_checked"] += 1
            for evidence in memory["evidence"]:
                if evidence["event_id"] not in events or evidence["history_id"] not in units:
                    raise ValueError("Memory History evidence is missing")
                result["history_events_checked"] += 1
            result["memories_checked"] += 1
        except (ValueError, KeyError) as exc:
            result["errors"].append(memory["id"] + ":" + str(exc))
    for record in records:
        payload = Insight.model_validate(record["payload"]).model_dump()
        role = payload["roles"][0] if payload["roles"] else "main"
        if payload["authority"] == "reviewer" and not payload["human_confirmed"]:
            role = "reviewer"
        if payload["category"] == "agent":
            role = "main"
        paths = list(payload["artifact_paths"])
        content_type = payload["content_types"][0] if payload["content_types"] else ""
        for anchor in payload["times"]:
            if anchor["dimension"] == "chapter" and anchor["start"]:
                paths.append("chapters/content_" + anchor["start"] + ".md")
        for solution in payload["solutions"]:
            paths.extend(solution["artifact_paths"])
            if not content_type and solution["content_types"]:
                content_type = solution["content_types"][0]
            if not payload["roles"] and solution["roles"]:
                role = solution["roles"][0]
        vector = np.frombuffer(record["vector"], dtype=np.float32).copy()
        class SavedVector:
            async def encode(self, texts):
                return np.stack([vector for _ in texts])
        retrieval = KnowledgeRetrieval(store, SavedVector(), record["model_id"])
        async def capture_failure(*args):
            result["errors"].append("runtime_reported_failure:" + record["id"] + ":" + str(args[2]))
        pipeline = SimpleNamespace(store=lambda _: store, retrieval=lambda _: retrieval, capture_failure=capture_failure)
        runtime = KnowledgeRuntime(pipeline, {"insight_tokens": 32000, "pattern_tokens": 32000,
                                               "insight_limit": max(20, len(records)), "pattern_promotion_weight": 200})
        try:
            for source in payload["sources"]:
                memory = store.get(source["memory_id"], source["version"])
                if not memory or memory["layer"] != "memory":
                    raise ValueError("Pinned source Memory version is missing")
                result["source_memory_versions_checked"] += 1
            for evidence in record["evidence"]:
                if evidence["event_id"] not in events or evidence["history_id"] not in units:
                    raise ValueError("Exact History evidence is missing")
                result["history_events_checked"] += 1
            assembled = await runtime.assemble("snapshot", payload["claim"], role=role, paths=paths, content_type=content_type)
            attachment = next(a for a in assembled["attachments"] if a["id"] == record["id"])
            if attachment["text"] not in assembled["text"] or payload["claim"] not in attachment["text"]:
                raise ValueError("Complete knowledge entry was not assembled")
            if set(assembled["recall"]["routes"]) != {"semantic", "keyword", "relation", "temporal"}:
                raise ValueError("Four-route retrieval did not execute")
            result["context_attachments_checked"] += 1
            before = store.view(store.get(record["id"]))["weight"]
            task = "offline-fixture:" + record["id"] + ":0"
            messages = [{"role": "system", "content": assembled["text"]}]
            runtime.mark_used("snapshot", [attachment], messages, task, role)
            after = store.view(store.get(record["id"]))["weight"]
            if after != min(300, before + 15):
                raise ValueError("Successful context use did not add 15 points")
            runtime.mark_used("snapshot", [attachment], messages, task, role)
            if store.view(store.get(record["id"]))["weight"] != after:
                raise ValueError("The same task was scored twice")
            result["score_and_dedup_checked"] += 1
            for index in range(1, 10):
                if store.get(record["id"])["layer"] == "pattern":
                    break
                runtime.mark_used("snapshot", [attachment], messages, "offline-fixture:" + record["id"] + f":{index}", role)
            if store.get(record["id"])["layer"] != "pattern":
                raise ValueError("Score threshold did not promote Pattern")
            assembled = await runtime.assemble("snapshot", "offline pattern assembly fixture", role=role, paths=paths, content_type=content_type)
            if not any(a["id"] == record["id"] and a["layer"] == "pattern" for a in assembled["attachments"]):
                raise ValueError("Mature Pattern was not assembled by default")
            result["pattern_default_checked"] += 1
            result["insights_checked"] += 1
        except (ValueError, StopIteration, KeyError) as exc:
            result["errors"].append(record["id"] + ":" + str(exc))
    result["verified"] = not result["errors"]
    return result
