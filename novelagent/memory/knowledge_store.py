"""Project-local knowledge authority. Vectors and publication share a transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import re
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from novelagent.memory.models import MemoryPackage, Insight, render_knowledge, time_key
from novelagent.history.sources import root_history_ids

INITIAL_KNOWLEDGE_WEIGHT = 65
RECALL_WEIGHT_BONUS = 15
PATTERN_PROMOTION_WEIGHT = 200
MAX_RECORD_WEIGHT = 300


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeStore:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).resolve()
        self.db_path = self.project_dir / ".knowledge" / "knowledge.sqlite3"

    @contextmanager
    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, layer TEXT NOT NULL, current_version INTEGER NOT NULL,
                published_version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active', updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT NOT NULL, version INTEGER NOT NULL, previous_version INTEGER NOT NULL,
                payload TEXT NOT NULL, evidence TEXT NOT NULL, model_id TEXT NOT NULL,
                vector BLOB NOT NULL, dimensions INTEGER NOT NULL, created TEXT NOT NULL,
                PRIMARY KEY(id,version), FOREIGN KEY(id) REFERENCES records(id));
            CREATE TABLE IF NOT EXISTS derivations (
                knowledge_id TEXT NOT NULL, knowledge_version INTEGER NOT NULL,
                memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(knowledge_id,knowledge_version,memory_id,memory_version,role),
                FOREIGN KEY(knowledge_id,knowledge_version) REFERENCES versions(id,version),
                FOREIGN KEY(memory_id,memory_version) REFERENCES versions(id,version));
            CREATE INDEX IF NOT EXISTS derived_memory ON derivations(memory_id,memory_version);
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entity_aliases (
                name TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entities(id), PRIMARY KEY(name,entity_id));
            CREATE TABLE IF NOT EXISTS entity_links (
                id TEXT NOT NULL, version INTEGER NOT NULL, fact_id TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entities(id),
                PRIMARY KEY(id,version,fact_id,entity_id), FOREIGN KEY(id,version) REFERENCES versions(id,version));
            CREATE INDEX IF NOT EXISTS entity_records ON entity_links(entity_id,id,version);
            CREATE TABLE IF NOT EXISTS edges (
                owner_id TEXT NOT NULL, owner_version INTEGER NOT NULL, target_id TEXT NOT NULL,
                target_version INTEGER NOT NULL, kind TEXT NOT NULL, properties TEXT NOT NULL,
                FOREIGN KEY(owner_id,owner_version) REFERENCES versions(id,version),
                FOREIGN KEY(target_id,target_version) REFERENCES versions(id,version));
            CREATE INDEX IF NOT EXISTS outgoing_edges ON edges(owner_id,owner_version,kind);
            CREATE INDEX IF NOT EXISTS incoming_edges ON edges(target_id,target_version,kind);
            CREATE TABLE IF NOT EXISTS times (
                id TEXT NOT NULL, version INTEGER NOT NULL, fact_id TEXT NOT NULL,
                dimension TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, properties TEXT NOT NULL,
                FOREIGN KEY(id,version) REFERENCES versions(id,version));
            CREATE INDEX IF NOT EXISTS time_ranges ON times(dimension,start,end);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY(id,version));
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, id TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS inputs (
                history_id TEXT NOT NULL, fingerprint TEXT NOT NULL, prepared TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', PRIMARY KEY(history_id,fingerprint));
            CREATE TABLE IF NOT EXISTS uses (
                id TEXT NOT NULL, version INTEGER NOT NULL, task_id TEXT NOT NULL,
                solution_id TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL,
                PRIMARY KEY(id,version,task_id));
            CREATE TABLE IF NOT EXISTS audits (
                audit_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_results (
                request_id TEXT PRIMARY KEY, stage TEXT NOT NULL, response TEXT NOT NULL,
                usage TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS migration_progress (
                history_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_replacements (
                old_id TEXT NOT NULL, old_version INTEGER NOT NULL, new_id TEXT NOT NULL, new_version INTEGER NOT NULL,
                reason TEXT NOT NULL, PRIMARY KEY(old_id,old_version,new_id,new_version),
                FOREIGN KEY(old_id,old_version) REFERENCES versions(id,version),
                FOREIGN KEY(new_id,new_version) REFERENCES versions(id,version));
            CREATE TABLE IF NOT EXISTS maturity (
                id TEXT PRIMARY KEY REFERENCES records(id), base_weight REAL NOT NULL DEFAULT 65);
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT NOT NULL, version INTEGER NOT NULL, fact_id TEXT NOT NULL,
                claim TEXT NOT NULL, status TEXT NOT NULL, domain TEXT NOT NULL, source_event_ids TEXT NOT NULL,
                PRIMARY KEY(id,version,fact_id), FOREIGN KEY(id,version) REFERENCES versions(id,version));
            CREATE TABLE IF NOT EXISTS fact_changes (
                id TEXT NOT NULL, previous_version INTEGER NOT NULL, version INTEGER NOT NULL,
                previous_fact_id TEXT NOT NULL, current_fact_id TEXT NOT NULL, kind TEXT NOT NULL,
                reason TEXT NOT NULL, source_event_ids TEXT NOT NULL,
                PRIMARY KEY(id,version,previous_fact_id,current_fact_id),
                FOREIGN KEY(id,version) REFERENCES versions(id,version));
        """)
        # Backfill only structural metadata from already saved payloads, never infer facts.
        for row in db.execute("SELECT id,version,payload FROM versions v WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.id=v.id AND f.version=v.version)").fetchall():
            payload = json.loads(row["payload"])
            metadata = payload.get("facts", [])
            for source in payload.get("sources", []):
                saved = db.execute("SELECT payload FROM versions WHERE id=? AND version=?", (source["memory_id"], source["version"])).fetchone()
                if saved:
                    metadata.extend(f for f in json.loads(saved[0])["facts"] if not source["fact_ids"] or f["fact_id"] in source["fact_ids"])
            for fact in metadata:
                db.execute("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?)", (row["id"], row["version"], fact["fact_id"], fact["claim"], fact.get("status", "active"), fact["domain"], json.dumps(fact["source_event_ids"])))
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if not row:
            return None
        record = {**dict(row), "payload": json.loads(row["payload"]), "evidence": json.loads(row["evidence"])}
        try:
            schema = MemoryPackage if record['layer'] == 'memory' else Insight
            record['payload'] = schema.model_validate(record['payload']).model_dump()
        except ValidationError as exc:
            # Preserve immutable raw evidence; an old invalid payload is readable but not usable.
            record['status'] = 'validation_failed'
            record['schema_errors'] = [{"loc": list(e['loc']), "type": e['type']} for e in exc.errors(include_input=False)]
        return record

    def get(self, record_id: str, version: int | None = None):
        if not self.db_path.exists():
            return None
        with self.connect() as db:
            return self._decode(db.execute("""SELECT r.*,v.version,v.previous_version,v.payload,v.evidence,
                v.model_id,v.vector,v.dimensions,v.created FROM records r JOIN versions v ON v.id=r.id
                WHERE r.id=? AND v.version=COALESCE(?,r.published_version)""", (record_id, version)).fetchone())

    def list(self, layers=("memory", "insight", "pattern"), include_inactive=False):
        if not self.db_path.exists():
            return []
        with self.connect() as db:
            records = [self._decode(row) for row in db.execute(f"""SELECT r.*,v.version,v.previous_version,
                v.payload,v.evidence,v.model_id,v.vector,v.dimensions,v.created FROM records r JOIN versions v
                ON v.id=r.id AND v.version=r.published_version WHERE r.layer IN ({','.join('?' for _ in layers)})
                {' ' if include_inactive else "AND r.status='active'"} ORDER BY r.updated DESC,r.id""", layers)]
            return records if include_inactive else [r for r in records if r['status'] == 'active']

    def derived(self, memory_id: str):
        if not self.db_path.exists():
            return []
        with self.connect() as db:
            ids = [r[0] for r in db.execute("""SELECT DISTINCT d.knowledge_id FROM derivations d JOIN records r
                ON r.id=d.knowledge_id AND r.published_version=d.knowledge_version WHERE d.memory_id=?""", (memory_id,))]
        return [self.get(record_id) for record_id in ids]

    def entity_candidates(self, names, kind):
        if not names or not self.db_path.exists():
            return []
        with self.connect() as db:
            rows = db.execute(f"""SELECT DISTINCT e.* FROM entities e JOIN entity_aliases a ON a.entity_id=e.id
                WHERE e.kind=? AND a.name IN ({','.join('?' for _ in names)}) ORDER BY e.id""", [kind, *names]).fetchall()
            result = []
            for entity in rows:
                contexts = []
                for row in db.execute("""SELECT DISTINCT v.payload FROM entity_links l JOIN records r ON r.id=l.id
                    JOIN versions v ON v.id=l.id AND v.version=l.version
                    WHERE l.entity_id=? AND r.layer='memory' AND r.published_version=l.version AND r.status='active' LIMIT 4""", (entity["id"],)):
                    payload = json.loads(row[0])
                    contexts.append({"background": payload["background"], "artifact_paths": payload["artifact_paths"],
                                     "facts": [f["claim"] for f in payload["facts"] if any(e["entity_id"] == entity["id"] for e in f["entities"])]})
                result.append({**dict(entity), "contexts": contexts})
            return result

    def save_memory(self, payload, evidence, vector, model_id, record_id="", expected_version=0, operation_id=""):
        return self.save_memories([{"payload": payload, "evidence": evidence, "vector": vector, "model_id": model_id,
                                   "id": record_id, "expected_version": expected_version, "operation_id": operation_id}])[0]

    def save_memories(self, entries):
        """Resolve same-batch record/fact aliases, then publish the whole batch atomically."""
        entries = deepcopy(entries)
        resolved, aliases, fact_aliases, identities = [], {}, {}, set()
        with self.connect() as db:
            for index, entry in enumerate(entries, 1):
                op = entry.get("operation_id", "")
                previous = db.execute("SELECT id,version FROM operations WHERE operation_id=?", (op,)).fetchone() if op else None
                rid, version = tuple(previous) if previous else (entry.get("id") or "mem_" + uuid.uuid4().hex, entry.get("expected_version", 0) + 1)
                if rid in identities:
                    raise ValueError("One batch cannot update the same Memory twice")
                identities.add(rid)
                alias = entry.get("batch_id", f"new:{index}")
                if alias in aliases:
                    raise ValueError("Duplicate batch Memory alias")
                aliases[alias] = (rid, version)
                payload = MemoryPackage.model_validate(entry["payload"]).model_dump()
                saved = json.loads(db.execute("SELECT payload FROM versions WHERE id=? AND version=?", (rid, version)).fetchone()[0]) if previous else None
                mapping, allocated = {}, set()
                for position, fact in enumerate(payload["facts"], 1):
                    key = fact["fact_id"] or f"new:{position}"
                    if re.fullmatch(r"new:\d+", key):
                        if key in mapping:
                            raise ValueError("Duplicate temporary fact identity")
                        stable = saved["facts"][position-1]["fact_id"] if saved else "fact_" + uuid.uuid4().hex
                        mapping[key] = stable
                        allocated.add(stable)
                        fact["fact_id"] = stable
                    elif previous:
                        # A partially committed legacy batch may be replayed with its saved stable IDs.
                        mapping[f"new:{position}"] = saved["facts"][position-1]["fact_id"]
                fact_aliases[alias] = mapping
                resolved.append({**entry, "id": rid, "version": version, "payload": payload, "allocated": allocated,
                                 "alias": alias, "skip": bool(previous)})
            entity_aliases = {}
            for entry in resolved:
                for fact in entry['payload']['facts']:
                    for entity in fact['entities']:
                        key = entity['entity_id']
                        if entry['skip']:
                            continue
                        if entity['ambiguous']:
                            if key.startswith('new:entity:'):
                                entity['entity_id'] = ''
                            continue
                        temporary = bool(re.fullmatch(r'new:entity:\d+', key))
                        # Unknown entities in different new packages must not merge by name alone.
                        if temporary or (len(resolved) > 1 and not key):
                            identity = entity_aliases.get(key) if temporary else None
                            if identity and identity[1] != entity['kind']:
                                raise ValueError('Batch entity identity conflicts with kind')
                            eid = identity[0] if identity else 'ent_' + uuid.uuid4().hex
                            if temporary:
                                entity_aliases[key] = (eid, entity['kind'])
                            db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?)', (eid, entity['name'], entity['kind']))
                            entity['entity_id'], entity['new_identity'] = eid, False
            for entry in resolved:
                if entry["skip"]:
                    continue
                payload = entry["payload"]
                local = fact_aliases[entry["alias"]]
                for change in payload["fact_changes"]:
                    change["current_fact_ids"] = [local.get(fid, fid) for fid in change["current_fact_ids"]]
                for relation in payload["relations"]:
                    target_alias = relation["target_id"]
                    internal = not target_alias
                    relation["source_fact_id"] = local.get(relation["source_fact_id"], relation["source_fact_id"])
                    if internal or target_alias in aliases:
                        target_alias = entry["alias"] if internal else target_alias
                        relation["target_id"], relation["target_version"] = aliases[target_alias]
                        relation["target_fact_id"] = fact_aliases[target_alias].get(relation["target_fact_id"], relation["target_fact_id"])
                    elif re.fullmatch(r"new:\d+", target_alias):
                        raise ValueError("Unknown batch Memory alias")
                self._put(db, "memory", payload, entry["evidence"], entry["vector"], entry["model_id"],
                          entry["id"], entry.get("expected_version", 0), allocated=entry["allocated"], defer_relations=True)
            for entry in resolved:
                if entry["skip"]:
                    continue
                rid, version = entry["id"], entry["version"]
                self._insert_relations(db, rid, version, entry["payload"]["relations"], entry["payload"]["facts"])
                db.execute("UPDATE jobs SET status='superseded' WHERE id=? AND version<? AND status IN ('pending','failed')", (rid, version))
                db.execute("INSERT OR IGNORE INTO jobs(id,version) VALUES (?,?)", (rid, version))
                if entry.get("operation_id"):
                    db.execute("INSERT INTO operations VALUES (?,?,?)", (entry["operation_id"], rid, version))
        return [self.get(e["id"], e["version"]) for e in resolved]

    def publish_insights(self, entries: list[dict], source_versions: dict[str, int], withdrawals=(), completed_jobs=None, withdrawal_versions=None):
        with self.connect() as db:
            for source_id, version in source_versions.items():
                source = db.execute("SELECT current_version,layer FROM records WHERE id=?", (source_id,)).fetchone()
                if not source or source[0] != version or source[1] != "memory":
                    raise ValueError("Source Memory changed during consolidation")
            updated_ids = [e["id"] for e in entries if e.get("id")]
            if len(updated_ids) != len(set(updated_ids)) or set(updated_ids) & set(withdrawals):
                raise ValueError("Conflicting publication operations")
            old_versions = {}
            for rid in withdrawals:
                old = db.execute("SELECT current_version,layer FROM records WHERE id=?", (rid,)).fetchone()
                if not old or old[1] not in {"insight", "pattern"}:
                    raise ValueError("Invalid knowledge withdrawal")
                if withdrawal_versions is not None and old[0] != withdrawal_versions.get(rid):
                    raise ValueError("Withdrawn knowledge changed during consolidation")
                old_versions[rid] = old[0]
            result_ids = []
            for entry in entries:
                record_id = entry.get("id") or "ins_" + uuid.uuid4().hex
                self._put(db, "insight", entry["payload"], entry["evidence"], entry["vector"], entry["model_id"],
                          record_id, entry.get("expected_version", 0))
                result_ids.append(record_id)
                for previous_id in entry.get("replaces", []):
                    if previous_id not in old_versions:
                        raise ValueError("Replacement must cite a withdrawn knowledge candidate")
                    version = entry.get("expected_version", 0) + 1
                    db.execute("INSERT INTO knowledge_replacements VALUES (?,?,?,?,?)", (previous_id, old_versions[previous_id],
                               record_id, version, entry.get("replacement_reason", "")))
                    db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?)", (record_id, version, previous_id, old_versions[previous_id],
                               "semantic", json.dumps({"predicate": "replaces", "inferred": False})))
                if entry.get("replaces"):
                    # Fusion is not a recall: preserve the highest existing score, never add scores.
                    weight = max(self._weight(db, rid) for rid in [record_id, *entry["replaces"]])
                    marks = ','.join('?' for _ in entry['replaces'])
                    for use in db.execute(f"SELECT * FROM uses WHERE id IN ({marks}) ORDER BY created", entry['replaces']).fetchall():
                        if not db.execute('SELECT 1 FROM uses WHERE id=? AND task_id=?', (record_id, use['task_id'])).fetchone():
                            # Inherited task identities prevent a fusion from replaying old recall points.
                            db.execute('INSERT INTO uses VALUES (?,?,?,?,?,?)', (record_id, entry.get('expected_version', 0)+1,
                                       use['task_id'], use['solution_id'], use['role'], use['created']))
                    count = self._use_count(db, record_id)
                    db.execute("INSERT INTO maturity VALUES (?,?) ON CONFLICT(id) DO UPDATE SET base_weight=excluded.base_weight",
                               (record_id, weight - RECALL_WEIGHT_BONUS * count))
                if self._weight(db, record_id) >= PATTERN_PROMOTION_WEIGHT:
                    db.execute("UPDATE records SET layer='pattern' WHERE id=? AND status='active'", (record_id,))
            for record_id in withdrawals:
                db.execute("UPDATE records SET status='withdrawn' WHERE id=? AND layer IN ('insight','pattern')", (record_id,))
            for source_id, version in (completed_jobs if completed_jobs is not None else source_versions).items():
                db.execute("UPDATE jobs SET status='complete',error='' WHERE id=? AND version=?", (source_id, version))
        return [self.get(record_id) for record_id in result_ids]

    def _put(self, db, layer, payload, evidence, vector, model_id, record_id, expected_version, *, allocated=(), defer_relations=False):
        schema = MemoryPackage if layer == "memory" else Insight
        payload = schema.model_validate(payload).model_dump()
        evidence_ids = {e["event_id"] for e in evidence}
        for fact in payload.get("facts", []):
            if set(fact["source_event_ids"]) - evidence_ids:
                raise ValueError("Fact cites unavailable evidence")
        for relation in payload["relations"]:
            if set(relation["source_event_ids"]) - evidence_ids:
                raise ValueError("Relation cites unavailable evidence")
        for change in payload.get("fact_changes", []):
            if set(change["source_event_ids"]) - evidence_ids:
                raise ValueError("Fact change cites unavailable evidence")
        current = db.execute("SELECT current_version,layer FROM records WHERE id=?", (record_id,)).fetchone()
        if (current[0] if current else 0) != expected_version:
            raise ValueError("Knowledge version conflict")
        if current and (current[1] == "memory") != (layer == "memory"):
            raise ValueError("Fact packages and learned knowledge cannot change identity")
        vector = np.asarray(vector, dtype=np.float32)
        if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or not np.linalg.norm(vector):
            raise ValueError("Invalid knowledge embedding")
        version = expected_version + 1
        stamp = now()
        if layer == "memory":
            old = db.execute("SELECT payload FROM versions WHERE id=? AND version=?", (record_id, expected_version)).fetchone()
            old_ids = {f["fact_id"] for f in json.loads(old[0])["facts"]} if old else set()
            new_ids = {}
            for index, fact in enumerate(payload["facts"], 1):
                if not fact["fact_id"]:
                    fact["fact_id"] = f"new:{index}"
                temporary = bool(re.fullmatch(r"new:\d+", fact["fact_id"]))
                if fact["fact_id"] and not temporary and fact["fact_id"] not in old_ids and fact["fact_id"] not in allocated:
                    raise ValueError("Unknown prior fact identity")
                if temporary:
                    if fact["fact_id"] in new_ids:
                        raise ValueError("Duplicate temporary fact identity")
                    new_ids[fact["fact_id"]] = "fact_" + uuid.uuid4().hex
                fact["fact_id"] = new_ids[fact["fact_id"]] if temporary else fact["fact_id"] or "fact_" + uuid.uuid4().hex
            for relation in payload["relations"]:
                relation["source_fact_id"] = new_ids.get(relation["source_fact_id"], relation["source_fact_id"])
                if not relation["target_id"]:
                    relation["target_fact_id"] = new_ids.get(relation["target_fact_id"], relation["target_fact_id"])
                    relation["target_id"], relation["target_version"] = record_id, version
            if len({f["fact_id"] for f in payload["facts"]}) != len(payload["facts"]):
                raise ValueError("Duplicate fact identity")
            active_ids = {f["fact_id"] for f in payload["facts"] if f["status"] == "active"}
            old_facts = {f["fact_id"]: f for f in json.loads(old[0])["facts"]} if old else {}
            changed_ids = set()
            for change in payload["fact_changes"]:
                change["current_fact_ids"] = [new_ids.get(fid, fid) for fid in change["current_fact_ids"]]
                previous_ids, current_ids = set(change["previous_fact_ids"]), set(change["current_fact_ids"])
                if previous_ids - old_ids or current_ids - active_ids:
                    raise ValueError("Fact change references an unknown versioned fact")
                if previous_ids & changed_ids:
                    raise ValueError("Conflicting fact changes")
                changed_ids.update(previous_ids)
                if change["kind"] == "withdrawn":
                    if current_ids or previous_ids & active_ids:
                        raise ValueError("Withdrawn facts cannot remain active")
                elif not current_ids:
                    raise ValueError("Fact replacement requires active successors")
                elif change["kind"] == "split" and (len(previous_ids) != 1 or len(current_ids) < 2):
                    raise ValueError("Fact split requires one predecessor and multiple successors")
                elif change["kind"] == "merged" and (len(previous_ids) < 2 or len(current_ids) != 1):
                    raise ValueError("Fact merge requires multiple predecessors and one successor")
                elif change["kind"] == "corrected" and (len(previous_ids) != 1 or len(current_ids) != 1):
                    raise ValueError("Fact correction requires one predecessor and one successor")
                if change["kind"] != "corrected" and previous_ids & active_ids:
                    raise ValueError("Replaced facts cannot remain active")
                if change["kind"] == "corrected" and (previous_ids - current_ids) & active_ids:
                    raise ValueError("Superseded facts cannot remain active")
            old_active = {f["fact_id"] for f in json.loads(old[0])["facts"] if f.get("status", "active") == "active"} if old else set()
            if old_active - active_ids - changed_ids:
                raise ValueError("Removed facts require an explicit withdrawal or replacement")
            for fact in payload["facts"]:
                prior = old_facts.get(fact["fact_id"])
                if prior and fact["status"] == "active" and (prior["claim"] != fact["claim"] or prior.get("status", "active") != "active") and fact["fact_id"] not in changed_ids:
                    raise ValueError("Changed or restored facts require an explicit correction")
        else:
            for solution in payload["solutions"]:
                solution["solution_id"] = solution["solution_id"] or "sol_" + uuid.uuid4().hex
            if len({s["solution_id"] for s in payload["solutions"]}) != len(payload["solutions"]):
                raise ValueError("Duplicate solution identity")
        db.execute("""INSERT INTO records VALUES (?,?,?,?,'active',?) ON CONFLICT(id) DO UPDATE SET
            layer=excluded.layer,current_version=excluded.current_version,published_version=excluded.published_version,
            status=excluded.status,updated=excluded.updated""", (record_id, layer, version, version, stamp))
        if layer != "memory":
            db.execute("UPDATE records SET status=? WHERE id=?", (payload["status"], record_id))
        db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)", (record_id, version, expected_version,
                   json.dumps(payload, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), model_id,
                   vector.tobytes(), len(vector), stamp))
        if layer != "memory":
            db.execute("INSERT OR IGNORE INTO maturity(id) VALUES (?)", (record_id,))
            if current and current[1] == "pattern" and payload["status"] == "active":
                db.execute("UPDATE records SET layer='pattern' WHERE id=?", (record_id,))
        facts = payload.get("facts", [])
        if layer != "memory":
            for source in payload["sources"]:
                row = db.execute("SELECT payload FROM versions WHERE id=? AND version=?", (source["memory_id"], source["version"])).fetchone()
                if not row:
                    raise ValueError("Source Memory version not found")
                source_facts = json.loads(row[0])["facts"]
                if set(source["fact_ids"]) - {f["fact_id"] for f in source_facts}:
                    raise ValueError("Source fact not found")
                db.execute("INSERT INTO derivations VALUES (?,?,?,?,?)", (record_id, version, source["memory_id"], source["version"], source["role"]))
                selected = [f for f in source_facts if not source["fact_ids"] or f["fact_id"] in source["fact_ids"]]
                if source["role"] == "support" and any(f.get("status", "active") != "active" for f in selected):
                    raise ValueError("Withdrawn facts cannot support current knowledge")
                facts.extend(selected)
                for fact in selected:
                    db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?)", (record_id, version, source["memory_id"], source["version"],
                               "semantic", json.dumps({"predicate": source["role"], "inferred": False,
                                "source_fact_id": fact["fact_id"], "target_fact_id": fact["fact_id"]})))
        for fact in facts:
            db.execute("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?)", (record_id, version, fact["fact_id"], fact["claim"],
                       fact.get("status", "active"), fact["domain"], json.dumps(fact["source_event_ids"])))
            if fact.get("status", "active") != "active":
                continue
            for entity in fact["entities"]:
                if entity["ambiguous"]:
                    continue
                eid = entity["entity_id"]
                if eid:
                    existing_entity = db.execute("SELECT kind FROM entities WHERE id=?", (eid,)).fetchone()
                    if not existing_entity:
                        raise ValueError("Unknown entity ID")
                    if existing_entity[0] != entity["kind"] or entity.get("new_identity", False):
                        raise ValueError("Entity identity conflicts with kind or new-identity flag")
                else:
                    matches = [] if entity.get("new_identity", False) else db.execute("SELECT entity_id FROM entity_aliases a JOIN entities e ON e.id=a.entity_id WHERE a.name=? AND e.kind=?",
                                                                           (entity["name"], entity["kind"])).fetchall()
                    if len(matches) > 1:
                        raise ValueError("Ambiguous entity needs explicit identity")
                    eid = matches[0][0] if matches else "ent_" + uuid.uuid4().hex
                    db.execute("INSERT OR IGNORE INTO entities VALUES (?,?,?)", (eid, entity["name"], entity["kind"]))
                for alias in [entity["name"], *entity["aliases"]]:
                    db.execute("INSERT OR IGNORE INTO entity_aliases VALUES (?,?)", (alias, eid))
                entity["entity_id"] = eid
                entity["new_identity"] = False
                db.execute("INSERT OR IGNORE INTO entity_links VALUES (?,?,?,?)", (record_id, version, fact["fact_id"], eid))
            for anchor in fact["times"]:
                if anchor["start"]:
                    start = time_key(anchor["dimension"], anchor["start"])
                    end = time_key(anchor["dimension"], anchor["end"] or anchor["start"])
                    if end < start:
                        raise ValueError("Reversed time range")
                    db.execute("INSERT INTO times VALUES (?,?,?,?,?,?,?)", (record_id, version, fact["fact_id"],
                               anchor["dimension"], start, end, json.dumps(anchor, ensure_ascii=False)))
        db.execute("INSERT INTO times VALUES (?,?,?,'created',?,?,?)", (record_id, version, "", stamp, stamp, '{}'))
        for anchor in payload.get("times", []):
            if anchor["start"]:
                start = time_key(anchor["dimension"], anchor["start"])
                end = time_key(anchor["dimension"], anchor["end"] or anchor["start"])
                if end < start:
                    raise ValueError("Reversed knowledge time range")
                db.execute("INSERT INTO times VALUES (?,?,?,?,?,?,?)", (record_id, version, "", anchor["dimension"],
                           start, end, json.dumps(anchor, ensure_ascii=False)))
        for change in payload.get("fact_changes", []):
            for previous_id in change["previous_fact_ids"]:
                for successor in change["current_fact_ids"] or [""]:
                    db.execute("INSERT INTO fact_changes VALUES (?,?,?,?,?,?,?,?)", (record_id, expected_version, version, previous_id,
                               successor, change["kind"], change["reason"], json.dumps(change["source_event_ids"])))
        if not defer_relations:
            self._insert_relations(db, record_id, version, payload["relations"], facts)
        # Entity IDs are resolved inside this transaction before the immutable version becomes visible.
        if layer == "memory":
            db.execute("UPDATE versions SET payload=? WHERE id=? AND version=?",
                       (json.dumps(payload, ensure_ascii=False), record_id, version))
        return {"id": record_id, "version": version}

    @staticmethod
    def _insert_relations(db, record_id, version, relations, facts):
        for relation in relations:
            if (relation["target_id"] == record_id and relation["target_version"] == version
                and (not relation["source_fact_id"] or not relation["target_fact_id"]
                     or relation["source_fact_id"] == relation["target_fact_id"])):
                raise ValueError("Self relation is not evidence")
            if relation["kind"] == "causal" and (not relation["source_fact_id"] or not relation["target_fact_id"]):
                raise ValueError("Causal relations require concrete source and target facts")
            target = db.execute("SELECT payload FROM versions WHERE id=? AND version=?", (relation["target_id"], relation["target_version"])).fetchone()
            if not target:
                raise ValueError("Relation target not found")
            if relation["source_fact_id"] and relation["source_fact_id"] not in {f["fact_id"] for f in facts}:
                raise ValueError("Relation source fact not found")
            target_facts = json.loads(target[0]).get("facts", [])
            if relation["target_fact_id"] and relation["target_fact_id"] not in {f["fact_id"] for f in target_facts}:
                raise ValueError("Relation target fact not found")
            if relation["status"] != "refuted" and (
                any(f["fact_id"] == relation["source_fact_id"] and f.get("status", "active") != "active" for f in facts)
                or any(f["fact_id"] == relation["target_fact_id"] and f.get("status", "active") != "active" for f in target_facts)):
                raise ValueError("Active relation cannot use withdrawn facts")
            db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?)", (record_id, version, relation["target_id"], relation["target_version"],
                       relation["kind"], json.dumps(relation, ensure_ascii=False)))

    def fact_history(self, memory_id, fact_id):
        """Exact predecessor/successor versions; never resolve by claim similarity."""
        if not self.db_path.exists():
            return {"versions": [], "changes": []}
        with self.connect() as db:
            versions = [dict(r) for r in db.execute("SELECT * FROM facts WHERE id=? AND fact_id=? ORDER BY version", (memory_id, fact_id))]
            changes = [dict(r) for r in db.execute("SELECT * FROM fact_changes WHERE id=? AND (previous_fact_id=? OR current_fact_id=?) ORDER BY version,previous_fact_id,current_fact_id", (memory_id, fact_id, fact_id))]
            for row in [*versions, *changes]:
                row["source_event_ids"] = json.loads(row["source_event_ids"])
            return {"versions": versions, "changes": changes}

    def graph(self):
        if not self.db_path.exists():
            return {"entities": [], "links": [], "edges": [], "times": [], "facts": []}
        with self.connect() as db:
            # Old source versions remain visible while published knowledge still cites them.
            visible = """SELECT id,published_version AS version FROM records WHERE status='active'
                UNION SELECT d.memory_id,d.memory_version FROM derivations d JOIN records r
                ON r.id=d.knowledge_id AND r.published_version=d.knowledge_version WHERE r.status='active'"""
            return self._active_graph({"entities": [dict(r) for r in db.execute("SELECT * FROM entity_aliases")],
                    "links": [dict(r) for r in db.execute(f"SELECT l.* FROM entity_links l JOIN ({visible}) v ON v.id=l.id AND v.version=l.version")],
                    "edges": [{**dict(r), "properties": json.loads(r["properties"])} for r in db.execute(
                        f"SELECT e.* FROM edges e JOIN ({visible}) v ON v.id=e.owner_id AND v.version=e.owner_version")],
                    "times": [dict(r) for r in db.execute(f"SELECT t.* FROM times t JOIN ({visible}) v ON v.id=t.id AND v.version=t.version")],
                    "facts": [dict(r) for r in db.execute(f"SELECT f.* FROM facts f JOIN ({visible}) v ON v.id=f.id AND v.version=f.version")]})

    @staticmethod
    def _active_graph(graph):
        withdrawn = {(f['id'], f['version'], f['fact_id']) for f in graph['facts'] if f['status'] != 'active'}
        members = {}
        for fact in graph['facts']:
            members.setdefault((fact['id'], fact['version']), set()).add(fact['fact_id'])
        projected = []
        for edge in graph['edges']:
            properties = edge['properties']
            if (edge['kind'] == 'semantic' and properties.get('predicate') in {'support', 'context', 'against'}
                and 'kind' not in properties and not properties.get('source_fact_id') and not properties.get('target_fact_id')):
                # Legacy derivation edges were record-level. Project only their actually cited facts.
                common = members.get((edge['owner_id'], edge['owner_version']), set()) & members.get((edge['target_id'], edge['target_version']), set())
                for fid in sorted(common):
                    projected.append({**edge, 'properties': {**properties, 'source_fact_id': fid, 'target_fact_id': fid}})
            else:
                projected.append(edge)
        graph['edges'] = projected
        graph['facts'] = [{**f, 'source_event_ids': json.loads(f['source_event_ids'])} for f in graph['facts'] if f['status'] == 'active']
        graph['links'] = [l for l in graph['links'] if (l['id'], l['version'], l['fact_id']) not in withdrawn]
        graph['times'] = [t for t in graph['times'] if (t['id'], t['version'], t['fact_id']) not in withdrawn]
        graph['edges'] = [e for e in graph['edges'] if (e['owner_id'], e['owner_version'], e['properties'].get('source_fact_id', '')) not in withdrawn
                          and (e['target_id'], e['target_version'], e['properties'].get('target_fact_id', '')) not in withdrawn]
        return graph

    def graph_neighborhood(self, seeds, query, temporal=None, *, depth=3, nodes=128, candidates=40):
        """Load a bounded SQL frontier, not the entire project's graph into Python."""
        empty = {"entities": [], "links": [], "edges": [], "times": [], "facts": []}
        if not self.db_path.exists():
            return empty
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE graph_visible(id TEXT, version INTEGER, PRIMARY KEY(id,version))")
            db.execute("""INSERT INTO graph_visible SELECT id,published_version FROM records WHERE status='active'
                UNION SELECT d.memory_id,d.memory_version FROM derivations d JOIN records r
                ON r.id=d.knowledge_id AND r.published_version=d.knowledge_version WHERE r.status='active'""")
            aliases = [dict(r) for r in db.execute("SELECT * FROM entity_aliases WHERE name!='' AND instr(?,name)>0 ORDER BY name,entity_id LIMIT ?", (query, candidates))]
            links, edges, times = {}, {}, []
            seeds = list(dict.fromkeys(tuple(key) for key in seeds))
            semantic_seeds = list(seeds)
            for alias in aliases:
                for row in db.execute("""SELECT l.* FROM entity_links l JOIN graph_visible v ON v.id=l.id AND v.version=l.version
                    WHERE l.entity_id=? AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.id=l.id AND f.version=l.version AND f.fact_id=l.fact_id AND f.status!='active')
                    ORDER BY l.id,l.version,l.fact_id LIMIT ?""", (alias['entity_id'], candidates)):
                    link = dict(row)
                    links[(link['id'], link['version'], link['fact_id'], link['entity_id'])] = link
                    seeds.append((link['id'], link['version']))
            if temporal:
                dimension = temporal['dimension']
                lo = time_key(dimension, temporal['start'])
                hi = time_key(dimension, temporal.get('end') or temporal['start'])
                if hi < lo:
                    raise ValueError('Reversed query time range')
                times = [dict(r) for r in db.execute("""SELECT t.* FROM times t JOIN graph_visible v ON v.id=t.id AND v.version=t.version
                    WHERE t.dimension=? AND t.start<=? AND t.end>=?
                    AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.id=t.id AND f.version=t.version AND f.fact_id=t.fact_id AND f.status!='active')
                    ORDER BY t.start DESC,t.id LIMIT ?""", (dimension, hi, lo, candidates))]
                # Reserve time's independent entry before a common entity fills the node budget.
                seeds = [*semantic_seeds, *((t['id'], t['version']) for t in times), *seeds]
            visited = set(list(dict.fromkeys(seeds))[:nodes])
            frontier = set(visited)
            db.execute('CREATE TEMP TABLE graph_frontier(id TEXT,version INTEGER,PRIMARY KEY(id,version))')
            for hop in range(depth + 1):
                if not frontier:
                    break
                db.execute('DELETE FROM graph_frontier')
                db.executemany('INSERT INTO graph_frontier VALUES (?,?)', sorted(frontier))
                discovered = set()
                limit = max(nodes * 4, candidates)
                for query_sql in (
                    'SELECT e.* FROM edges e JOIN graph_frontier f ON f.id=e.owner_id AND f.version=e.owner_version JOIN graph_visible v ON v.id=e.owner_id AND v.version=e.owner_version JOIN graph_visible t ON t.id=e.target_id AND t.version=e.target_version ORDER BY e.owner_id,e.target_id LIMIT ?',
                    'SELECT e.* FROM edges e JOIN graph_frontier f ON f.id=e.target_id AND f.version=e.target_version JOIN graph_visible v ON v.id=e.owner_id AND v.version=e.owner_version JOIN graph_visible t ON t.id=e.target_id AND t.version=e.target_version ORDER BY e.owner_id,e.target_id LIMIT ?',
                ):
                    for row in db.execute(query_sql, (limit,)):
                        edge = dict(row)
                        properties = json.loads(edge['properties'])
                        if properties.get('confidence', 1) < .5 or properties.get('status') == 'refuted':
                            continue
                        if db.execute("""SELECT 1 FROM facts WHERE status!='active' AND
                            ((id=? AND version=? AND fact_id=?) OR (id=? AND version=? AND fact_id=?)) LIMIT 1""",
                            (edge['owner_id'], edge['owner_version'], properties.get('source_fact_id', ''),
                             edge['target_id'], edge['target_version'], properties.get('target_fact_id', ''))).fetchone():
                            continue
                        key = (edge['owner_id'], edge['owner_version'], edge['target_id'], edge['target_version'], edge['kind'], edge['properties'])
                        edges[key] = {**edge, 'properties': properties}
                        discovered.update([(edge['owner_id'], edge['owner_version']), (edge['target_id'], edge['target_version'])])
                entity_ids = set()
                for row in db.execute("""SELECT l.* FROM entity_links l JOIN graph_frontier n ON n.id=l.id AND n.version=l.version
                    WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.id=l.id AND f.version=l.version AND f.fact_id=l.fact_id AND f.status!='active')
                    ORDER BY l.id,l.fact_id LIMIT ?""", (limit,)):
                    link = dict(row)
                    links[(link['id'], link['version'], link['fact_id'], link['entity_id'])] = link
                    entity_ids.add(link['entity_id'])
                for eid in sorted(entity_ids)[:nodes]:
                    for row in db.execute("""SELECT l.* FROM entity_links l JOIN graph_visible v ON v.id=l.id AND v.version=l.version WHERE l.entity_id=?
                        AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.id=l.id AND f.version=l.version AND f.fact_id=l.fact_id AND f.status!='active')
                        ORDER BY l.id,l.version LIMIT ?""", (eid, candidates)):
                        link = dict(row)
                        links[(link['id'], link['version'], link['fact_id'], link['entity_id'])] = link
                        discovered.add((link['id'], link['version']))
                if hop == depth:
                    break
                frontier = set(sorted(discovered - visited)[:max(0, nodes-len(visited))])
                visited.update(frontier)
            links = [l for l in links.values() if (l['id'], l['version']) in visited]
            edges = [e for e in edges.values() if (e['owner_id'], e['owner_version']) in visited and (e['target_id'], e['target_version']) in visited]
            db.execute('DELETE FROM graph_frontier')
            db.executemany('INSERT INTO graph_frontier VALUES (?,?)', sorted(visited))
            facts = [dict(r) for r in db.execute('SELECT f.* FROM facts f JOIN graph_frontier n ON n.id=f.id AND n.version=f.version')]
            return self._active_graph({'entities': aliases, 'links': links, 'edges': edges, 'times': times, 'facts': facts,
                                      'loaded_nodes': len(visited)})

    def input_state(self, history_id, digest, **updates):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO inputs(history_id,fingerprint) VALUES (?,?)", (history_id, digest))
            for key, value in updates.items():
                if key not in {"prepared", "status", "error"}:
                    raise ValueError("Invalid input state field")
                db.execute(f"UPDATE inputs SET {key}=? WHERE history_id=? AND fingerprint=?",
                           (json.dumps(value, ensure_ascii=False) if key == "prepared" else value, history_id, digest))
            row = dict(db.execute("SELECT * FROM inputs WHERE history_id=? AND fingerprint=?", (history_id, digest)).fetchone())
        row["prepared"] = json.loads(row["prepared"])
        return row

    def model_result(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT response,usage FROM model_results WHERE request_id=?", (request_id,)).fetchone()
        return {"response": row[0], "usage": json.loads(row[1])} if row else None

    def save_model_result(self, request_id, stage, response, usage, *, replace_invalid=False):
        with self.connect() as db:
            db.execute(f"INSERT OR {'REPLACE' if replace_invalid else 'IGNORE'} INTO model_results VALUES (?,?,?,?,?)",
                       (request_id, stage, response, json.dumps(usage), now()))

    def has_input(self, history_id):
        if not self.db_path.exists():
            return False
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM inputs WHERE history_id=? LIMIT 1", (history_id,)).fetchone())

    def operation(self, operation_id):
        if not self.db_path.exists():
            return None
        with self.connect() as db:
            row = db.execute("SELECT id,version FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        return self.get(row[0], row[1]) if row else None

    def has_operations(self, prefix):
        if not self.db_path.exists():
            return False
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM operations WHERE substr(operation_id,1,?)=? LIMIT 1",
                                   (len(prefix), prefix)).fetchone())

    def jobs(self):
        if not self.db_path.exists():
            return []
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status IN ('pending','failed')")]

    def fail_job(self, record_id, version, error):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=? AND version=? AND status!='superseded'", (error, record_id, version))

    def audit(self, payload):
        audit_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO audits VALUES (?,?,?)", (audit_id, json.dumps(payload, ensure_ascii=False), now()))
        return audit_id

    def migration_progress(self, history_id, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO migration_progress VALUES (?,?,?)",
                       (history_id, json.dumps(payload, ensure_ascii=False), now()))

    @staticmethod
    def _use_count(db, record_id):
        return db.execute("SELECT count(DISTINCT task_id) FROM uses WHERE id=?", (record_id,)).fetchone()[0]

    @classmethod
    def _weight(cls, db, record_id):
        row = db.execute("SELECT base_weight FROM maturity WHERE id=?", (record_id,)).fetchone()
        return max(0, min(MAX_RECORD_WEIGHT, (row[0] if row else INITIAL_KNOWLEDGE_WEIGHT) + RECALL_WEIGHT_BONUS * cls._use_count(db, record_id)))

    def mark_used(self, record_id, version, task_id, solution_id, role, threshold=0, *, score_threshold=PATTERN_PROMOTION_WEIGHT):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if not row or row["published_version"] != version or row["status"] != "active" or row["layer"] != "insight":
                return 0
            if not db.execute("SELECT 1 FROM uses WHERE id=? AND task_id=?", (record_id, task_id)).fetchone():
                db.execute("INSERT INTO uses VALUES (?,?,?,?,?,?)", (record_id, version, task_id, solution_id, role, now()))
            count = self._use_count(db, record_id)
            eligible = count >= threshold if threshold else self._weight(db, record_id) >= score_threshold
            if eligible:
                db.execute("UPDATE records SET layer='pattern' WHERE id=?", (record_id,))
            return count

    def merge_task_uses(self, root_id, attempt_ids, threshold=0, *, score_threshold=PATTERN_PROMOTION_WEIGHT):
        """A resumed demand is not a new independent use or verification."""
        aliases = list(dict.fromkeys(hid for hid in attempt_ids if hid != root_id))
        if not aliases or not self.db_path.exists():
            return
        with self.connect() as db:
            marks = ','.join('?' for _ in aliases)
            uses = [dict(r) for r in db.execute(f"SELECT * FROM uses WHERE task_id IN ({marks}) ORDER BY created", aliases)]
            db.execute(f"DELETE FROM uses WHERE task_id IN ({marks})", aliases)
            for use in uses:
                if not db.execute("SELECT 1 FROM uses WHERE id=? AND task_id=?", (use['id'], root_id)).fetchone():
                    db.execute('INSERT INTO uses VALUES (?,?,?,?,?,?)', (use['id'], use['version'], root_id, use['solution_id'], use['role'], use['created']))
            for rid in {u['id'] for u in uses}:
                count = self._use_count(db, rid)
                eligible = count >= threshold if threshold else self._weight(db, rid) >= score_threshold
                if not eligible:
                    db.execute("UPDATE records SET layer='insight' WHERE id=? AND layer='pattern'", (rid,))

    def view(self, record):
        payload = record["payload"]
        source_ids = [s["history_id"] for s in record["evidence"]]
        with self.connect() as db:
            used = self._use_count(db, record["id"])
            weight = self._weight(db, record["id"]) if record['layer'] != 'memory' else 0
            replacements = [dict(r) for r in db.execute("SELECT new_id,new_version,reason FROM knowledge_replacements WHERE old_id=? AND old_version=?",
                                                       (record["id"], record["version"]))]
        return {"id": record["id"], "layer": record["layer"], "version": record["version"],
                "previous_version": record["previous_version"], "schema": "knowledge_v1", "status": record["status"],
                "title": payload.get("title", "待核验历史知识"), "claim": payload.get("claim", payload.get("direction", "")),
                "category": payload.get("category", "project"), "domain": payload.get("domain", "overall"), "kind": payload.get("kind", "fact_package"),
                "weight": weight, "updated": record["updated"], "file_path": "",
                "content": ("此历史知识结构未通过当前校验，仅供核验，不参与召回。\n\n" + json.dumps(payload, ensure_ascii=False)
                            if record.get('schema_errors') else render_knowledge(record)),
                "support_count": used,
                "history_ids": root_history_ids(self.project_dir, source_ids), "payload": payload,
                "history_event_ids": [e["event_id"] for e in record["evidence"]],
                "evidence": record["evidence"], "memory_sources": payload.get("sources", []), "replacements": replacements,
                "schema_errors": record.get('schema_errors', [])}
