"""Explicit, bounded re-extraction of legacy sources; never relabel old layers."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import yaml

from novelagent.history.sources import root_history_ids
from novelagent.history.store import DocumentHistoryStore


def audit(project):
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("Project directory does not exist")
    history_path = project / ".history" / "history.sqlite3"
    histories = set()
    if history_path.exists():
        with closing(sqlite3.connect(f"file:{history_path.as_posix()}?mode=ro", uri=True)) as db:
            histories = {r[0] for r in db.execute("SELECT history_id FROM history_units")}
    counts, candidates, errors = Counter(), [], []
    for layer in ("insight", "memory", "pattern"):
        for path in sorted((project / ("."+layer)).rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
                if not text.startswith("---"):
                    continue
                header = yaml.safe_load(text.split("---", 2)[1]) or {}
                if not header.get("id"):
                    continue
                counts[layer] += 1
                ids = list(dict.fromkeys([*header.get("history_ids", []),
                    *(e["history_id"] for e in header.get("semantic_evidence", [])
                      if isinstance(e, dict) and e.get("history_id"))]))
                candidates.append({"legacy_id": header["id"], "layer": layer,
                    "path": str(path.relative_to(project)), "history_ids": ids,
                    "event_ids": list(dict.fromkeys([*header.get("history_event_ids", []),
                        *(eid for e in header.get("semantic_evidence", []) if isinstance(e, dict)
                          for eid in e.get("history_event_ids", []))])),
                    "missing_history_ids": [hid for hid in ids if hid not in histories],
                    "action": "reanalyze_history" if ids and all(hid in histories for hid in ids) else "verify_sources_first"})
            except (OSError, ValueError, yaml.YAMLError, TypeError) as exc:
                errors.append({"path": str(path.relative_to(project)), "error": str(exc)})
    return {"project": str(project), "mode": "read_only", "legacy_counts": dict(counts),
            "history_count": len(histories), "candidates": candidates, "errors": errors,
            "note": "Old layers cannot be mapped by rename; no extraction or migration has been performed."}


def preview(project):
    """Read-only even on an old schema: no store initialization or schema migration."""
    report = audit(project)
    project = Path(project).resolve()
    targets = root_history_ids(project, [hid for row in report["candidates"]
                                       if row["action"] == "reanalyze_history" for hid in row["history_ids"]])
    history_path = project / ".history/history.sqlite3"
    units = {}
    if history_path.exists():
        with closing(sqlite3.connect(f"file:{history_path.as_posix()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            units = {r["history_id"]: dict(r) for r in db.execute("SELECT * FROM history_units")}
    mappings, processed = [], {}
    knowledge_path = project / ".knowledge/knowledge.sqlite3"
    if knowledge_path.exists():
        with closing(sqlite3.connect(f"file:{knowledge_path.as_posix()}?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='migration_progress'").fetchone():
                rows = list(db.execute("SELECT history_id,payload FROM migration_progress ORDER BY updated"))
                processed = {root_history_ids(project, [r[0]])[0]: json.loads(r[1]) for r in rows}
            memories = [(r[0], r[1], json.loads(r[2])) for r in db.execute("""SELECT r.id,v.version,v.evidence
                FROM records r JOIN versions v ON v.id=r.id AND v.version=r.published_version WHERE r.layer='memory'""")]
        for row in report["candidates"]:
            required = set(row["event_ids"])
            matches = [{"memory_id": rid, "version": version,
                        "matched_event_ids": sorted(required & {e["event_id"] for e in evidence})}
                       for rid, version, evidence in memories if required & {e["event_id"] for e in evidence}]
            mappings.append({"legacy_id": row["legacy_id"], "candidates": matches,
                             "match": "exact_event_overlap" if matches else "unmapped",
                             "confirmed_replacement": False})
    jobs = []
    for hid in targets:
        unit = units.get(hid, {})
        eligible = unit.get("kind") in {"requirement", "conversation", "review", "document"} and (
            unit.get("status") == "completed" or (unit.get("kind") in {"requirement", "conversation"} and unit.get("status") in {"failed", "interrupted"}))
        if unit.get("kind") == "review":
            workflow = json.loads(unit.get("review_workflow_json") or "{}")
            eligible = eligible and (not workflow or workflow.get("status") == "completed")
        if unit.get("parent_request_id") and unit.get("kind") not in {"review", "document"}:
            eligible = False
        jobs.append({"history_id": hid, "eligible": eligible, "kind": unit.get("kind"),
                     "status": unit.get("status"), "progress": processed.get(hid, {})})
    return {**report, "targets": jobs, "source_mappings": mappings,
            "note": "Preview does not call models or write data. Evidence overlap is not a confirmed semantic replacement; legacy records remain readable."}


class KnowledgeMigration:
    def __init__(self, analyzer):
        self.analyzer = analyzer
        self.pipeline = analyzer.knowledge_pipeline

    def validate(self, project_id, history_ids):
        project = self.pipeline.store(project_id).project_dir
        allowed = {j["history_id"] for j in preview(project)["targets"] if j["eligible"]}
        ids = list(dict.fromkeys(history_ids))
        if not ids or len(ids) > 5 or set(root_history_ids(project, ids)) - allowed:
            raise ValueError("Select one to five eligible History IDs from the migration preview")
        return ids

    async def run(self, project_id, history_ids):
        ids = self.validate(project_id, history_ids)
        store = self.pipeline.store(project_id)
        history = DocumentHistoryStore(store.project_dir)
        results = []
        for hid in ids:
            store.migration_progress(hid, {"status": "running"})
            try:
                await self.analyzer.analyze(project_id, hid, force=True)
                root = history.get_unit(hid)
                if root["analysis_status"] != "complete" or not store.has_input(root["history_id"]):
                    raise ValueError("History extraction deferred or incomplete")
                # A completed retain input does not imply completed independent learning.
                await self.pipeline.consolidate_pending(project_id)
                root_ids = {a["history_id"] for a in root.get("attempts", [])} | {root["history_id"], hid}
                memories = [m for m in store.list(("memory",)) if root_ids & set(root_history_ids(store.project_dir,
                            [e["history_id"] for e in m["evidence"]]))]
                pending = [j for j in store.jobs() if any(m["id"] == j["id"] and m["version"] == j["version"] for m in memories)]
                result = {"status": "learning_pending" if pending else "complete", "canonical_history_id": root["history_id"],
                          "memories": [{"id": m["id"], "version": m["version"]} for m in memories], "pending_jobs": pending}
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}
            store.migration_progress(hid, result)
            results.append({"history_id": hid, **result})
        return results

    async def recover(self, project_id):
        store = self.pipeline.store(project_id)
        if not store.db_path.exists():
            return
        with store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT history_id,payload FROM migration_progress")
                   if json.loads(r[1]).get("status") in {"running", "learning_pending"}]
        # Only previously authorized work resumes. Failed runs require explicit retry.
        allowed = {j["history_id"] for j in preview(store.project_dir)["targets"] if j["eligible"]}
        ids = [hid for hid in ids if set(root_history_ids(store.project_dir, [hid])) <= allowed]
        for start in range(0, len(ids), 5):
            await self.run(project_id, ids[start:start+5])
