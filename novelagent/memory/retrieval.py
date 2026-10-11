"""One four-route engine for task recall, direction matching and reconciliation."""

import asyncio
import heapq
import math
import re
import time
from datetime import datetime, timezone
from collections import Counter, defaultdict

import numpy as np

from novelagent.memory.models import retrieval_text, time_key
from novelagent.rag.store import tokenize, reciprocal_rank_fusion
from novelagent.memory.index_cache import load_index, save_index


class KnowledgeRetrieval:
    def __init__(self, store, embedding_model, model_id, *, candidate_limit=40, graph_depth=3, graph_nodes=128, timeout=30):
        self.store = store
        self.embedding = embedding_model
        self.model_id = model_id
        self.candidate_limit = candidate_limit
        self.graph_depth = graph_depth
        self.graph_nodes = graph_nodes
        self.timeout = timeout
        self._index = None
        self._signature = None
        self._index_rows = []

    @staticmethod
    def applicable(record, role="", paths=(), content_type=""):
        p = record["payload"]
        if p.get("roles") and role and role not in p["roles"]:
            return False
        if p.get("authority") == "reviewer" and not p.get("human_confirmed") and role and role != "reviewer":
            return False
        if p.get("artifact_paths") and not set(p["artifact_paths"]) & set(paths):
            return False
        if p.get("content_types") and content_type not in p["content_types"]:
            return False
        if p.get("solutions") and not any(s["status"] == "active" for s in p["solutions"]):
            return False
        current = datetime.now(timezone.utc).isoformat()
        for anchor in p.get("times", []):
            if anchor["dimension"] == "valid":
                if anchor["start"] and time_key("valid", anchor["start"]) > current:
                    return False
                if anchor["end"] and time_key("valid", anchor["end"]) < current:
                    return False
            elif anchor["dimension"] == "chapter" and anchor["start"]:
                lo = time_key("chapter", anchor["start"])
                hi = time_key("chapter", anchor["end"] or anchor["start"])
                positions = [m[1] for path in paths if (m := re.search(r"(\d+\.\d+\.\d+)\.md$", path))]
                if not any(lo <= time_key("chapter", pos) <= hi for pos in positions):
                    return False
        return True

    async def recall(self, query, *, purpose="task_insight", role="", paths=(), temporal=None, limit=8, content_type=""):
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._recall(query, purpose, role, paths, temporal, limit, content_type), self.timeout)
        except asyncio.TimeoutError:
            result = {"status": "timeout", "items": [], "error": "Knowledge recall timed out", "routes": {}}
        except Exception as exc:
            result = {"status": "failed", "items": [], "error": str(exc), "routes": {}}
        result.update(purpose=purpose, query=query, elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        try:
            result["audit_id"] = self.store.audit({k: v for k, v in result.items() if k != "items"} | {
                "candidates": [{"id": r["record"]["id"], "version": r["record"]["version"], "routes": r["ranks"]} for r in result["items"]]})
        except Exception as exc:
            result.update(status="failed", items=[], error=f"Recall audit failed: {exc}", audit_id="")
        return result

    async def _recall(self, query, purpose, role, paths, temporal, limit, content_type):
        layers = {"task_insight": ("insight",), "memory_match": ("memory",),
                  "memory_evidence": ("memory",), "knowledge_reconcile": ("insight", "pattern")}
        if purpose not in layers:
            raise ValueError("Unknown knowledge recall purpose")
        rows = [r for r in self.store.list(layers[purpose])
                if (purpose != "task_insight" or self.applicable(r, role, paths, content_type))
                and (r["layer"] != "memory" or any(f.get("status", "active") == "active" for f in r["payload"]["statements"]))]
        if not rows:
            return {"status": "empty", "items": [], "routes": {k: {"count": 0, "elapsed_ms": 0} for k in ("semantic", "keyword", "relation", "temporal")}}
        routes, timings = {}, {}
        start = time.perf_counter()
        q = np.asarray((await self.embedding.encode([query]))[0], dtype=np.float32)
        matches, index_cache = await self._vector_matches(rows, q, purpose)
        semantic_scores = {rows[int(m.key)]["id"]: 1-float(m.distance) for m in matches if 1-float(m.distance) > 0}
        routes["semantic"] = list(semantic_scores)
        timings["semantic"] = (time.perf_counter()-start)*1000
        start = time.perf_counter()
        query_terms = Counter(tokenize(query))
        docs = {r["id"]: Counter(tokenize(retrieval_text(r["layer"], r["payload"]))) for r in rows}
        avg = sum(sum(d.values()) for d in docs.values()) / len(docs)
        df = Counter(t for doc in docs.values() for t in doc)
        scores = {}
        for rid, doc in docs.items():
            score = sum(math.log(1+(len(docs)-df[t]+.5)/(df[t]+.5)) *
                        (doc[t]*2.5)/(doc[t]+1.5*(.25+.75*sum(doc.values())/max(avg, 1))) * min(tf, 2)
                        for t, tf in query_terms.items() if doc[t])
            if score > 0:
                scores[rid] = score
        routes["keyword"] = sorted(scores, key=scores.get, reverse=True)[:self.candidate_limit]
        timings["keyword"] = (time.perf_counter()-start)*1000
        start = time.perf_counter()
        temporal = temporal or self.infer_temporal(query, paths)
        initial_ids = set(routes["semantic"][:8] + routes["keyword"][:8])
        initial_keys = [(r["id"], r["version"]) for r in rows if r["id"] in initial_ids]
        graph = await asyncio.to_thread(self.store.graph_neighborhood, initial_keys, query, temporal,
                                       depth=self.graph_depth, nodes=self.graph_nodes, candidates=self.candidate_limit)
        adjacency, members, entity_nodes = self._statement_graph(graph)
        seeds = {(rid, version, ""): ["seed"] for rid, version in initial_keys}
        for key in initial_keys:
            for node in members[key]:
                seeds[node] = ["seed", self._node_label(node)]
        for alias in graph["entities"]:
            if alias["name"] and alias["name"] in query:
                for node in sorted(entity_nodes[alias["entity_id"]])[:self.candidate_limit]:
                    seeds[node] = [f"entity:{alias['name']}", self._node_label(node)]
        record_keys = {(r["id"], r["version"]): r["id"] for r in rows}
        relation_paths = self._walk_graph(seeds, adjacency, entity_nodes, graph["links"], record_keys)
        routes["relation"] = sorted(relation_paths, key=lambda rid: (len(relation_paths[rid]), rid))[:self.candidate_limit]
        timings["relation"] = (time.perf_counter()-start)*1000
        start = time.perf_counter()
        temporal = temporal or self.infer_temporal(query, paths)
        time_hits = {}
        temporal_paths = {}
        if temporal:
            dimension = temporal["dimension"]
            lo = time_key(dimension, temporal["start"])
            hi = time_key(dimension, temporal.get("end") or temporal["start"])
            if hi < lo:
                raise ValueError("Reversed query time range")
            temporal_seeds = {}
            for anchor in graph["times"]:
                key = (anchor["id"], anchor["version"])
                if anchor["dimension"] == dimension and anchor["start"] <= hi and anchor["end"] >= lo:
                    node = (*key, anchor["statement_id"])
                    if node[2] and node not in members[key]:
                        continue
                    temporal_seeds[node] = ["time_anchor", self._node_label(node)]
                    if key in record_keys:
                        time_hits[record_keys[key]] = {"dimension": dimension, "start": anchor["start"], "end": anchor["end"]}
            # Evidence edges project timed Memory statements onto their derived Insight.
            temporal_paths = self._walk_graph(temporal_seeds, adjacency, {}, [], record_keys, temporal=True)
        routes["temporal"] = sorted(temporal_paths, key=lambda rid: (len(temporal_paths[rid]), -scores.get(rid, 0), rid))[:self.candidate_limit]
        timings["temporal"] = (time.perf_counter()-start)*1000
        rankings = [list(dict.fromkeys(routes[name])) for name in ("semantic", "keyword", "relation", "temporal")]
        fused = reciprocal_rank_fusion(*rankings)
        ranked = sorted(fused, key=lambda rid: (-fused[rid], rid))
        if temporal and temporal.get("hard"):
            ranked = [rid for rid in ranked if rid in time_hits]
        by_id = {r["id"]: r for r in rows}
        items = [{"record": by_id[rid], "score": fused[rid], "ranks": {name: routes[name].index(rid)+1 for name in routes if rid in routes[name]},
                  "paths": relation_paths.get(rid, []), "temporal": time_hits.get(rid),
                  "temporal_path": temporal_paths.get(rid, []), "similarity": semantic_scores.get(rid)}
                 for rid in ranked[:limit]]
        return {"status": "ok" if items else "empty", "items": items, "index_cache": index_cache,
                "graph_loaded_nodes": graph.get('loaded_nodes', 0),
                "routes": {name: {"count": len(routes[name]), "elapsed_ms": round(timings[name], 3)} for name in routes}}

    async def vector_candidates(self, vector, *, limit=20, exclude=()):
        """Pure cosine ranking for deduplication/graph candidates, never RRF scores."""
        started = time.perf_counter()
        excluded = set(exclude)
        async def search():
            rows = [r for r in self.store.list(("memory",)) if r["id"] not in excluded
                    and any(f["status"] == "active" for f in r["payload"]["statements"])]
            if not rows or limit <= 0:
                return []
            q = np.asarray(vector, dtype=np.float32)
            matches, _ = await self._vector_matches(rows, q, "memory_vector", limit)
            items = []
            for match in matches:
                row = rows[int(match.key)]
                v = np.frombuffer(row["vector"], dtype=np.float32)
                # Exact cosine for the strict merge gate, not approximate search distance.
                similarity = float(np.dot(q.astype(np.float64), v.astype(np.float64)) /
                                   (np.linalg.norm(q.astype(np.float64))*np.linalg.norm(v.astype(np.float64))))
                items.append({"record": row, "similarity": similarity})
            return sorted(items, key=lambda i: (-i["similarity"], i["record"]["id"]))[:limit]
        items = await asyncio.wait_for(search(), self.timeout)
        self.store.audit({"purpose": "memory_vector", "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                          "candidates": [{"id": i["record"]["id"], "version": i["record"]["version"],
                                          "similarity": i["similarity"]} for i in items]})
        return items

    async def _vector_matches(self, rows, q, purpose, limit=None):
        if any(r["model_id"] != self.model_id for r in rows):
            raise ValueError("Embedding model changed; rebuild knowledge vectors before recall")
        signature = tuple((r["id"], r["version"]) for r in rows)
        index = self._index
        index_cache = {"source": "memory"}
        if signature != self._signature:
            from usearch.index import Index
            dims = rows[0]["dimensions"]
            if any(r["dimensions"] != dims for r in rows):
                raise ValueError("Inconsistent knowledge embedding dimensions")
            vectors = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
            def build_index():
                identity = {"model_id": self.model_id, "dimensions": dims, "rows": [list(item) for item in signature]}
                directory = self.store.project_dir / '.knowledge/indexes'
                restored, reason = load_index(directory, purpose, identity)
                if restored is not None:
                    return restored, {"source": "persistent"}
                built = Index(ndim=dims, metric="cos", dtype="f32", connectivity=16, expansion_add=128, expansion_search=64)
                built.add(np.arange(len(rows), dtype=np.uint64), vectors)
                metadata = {"source": "rebuilt", "reason": reason}
                try:
                    save_index(directory, purpose, identity, built)
                except Exception as exc:
                    metadata["write_error"] = str(exc)
                return built, metadata
            index, index_cache = await asyncio.to_thread(build_index)
            self._index, self._signature, self._index_rows = index, signature, rows
        if q.shape != (rows[0]["dimensions"],) or not np.isfinite(q).all() or not np.linalg.norm(q):
            raise ValueError("Invalid query embedding")
        # Keep this request's index: concurrent background matching uses another pool.
        matches = await asyncio.to_thread(index.search, q, min(limit or self.candidate_limit, len(rows)))
        return matches, index_cache

    @staticmethod
    def _node_label(node):
        return f"{node[0]}@{node[1]}" + (f"#{node[2]}" if node[2] else "")

    @staticmethod
    def _statement_graph(graph):
        """Keep distinct statements distinct, including causal edges within one package."""
        adjacency, members, entity_nodes = defaultdict(list), defaultdict(set), defaultdict(set)
        inactive = {(f["id"], f["version"], f["statement_id"]) for f in graph.get("statements", []) if f.get("status", "active") != "active"}
        for fact in graph.get("statements", []):
            node = (fact["id"], fact["version"], fact["statement_id"])
            if node not in inactive:
                members[node[:2]].add(node)
        for link in graph["links"]:
            node = (link["id"], link["version"], link["statement_id"])
            if node not in inactive:
                entity_nodes[link["entity_id"]].add(node)
                members[node[:2]].add(node)
        for anchor in graph["times"]:
            node = (anchor["id"], anchor["version"], anchor["statement_id"])
            if node[2] and node not in inactive:
                members[node[:2]].add(node)
        for edge in graph["edges"]:
            properties = edge["properties"]
            if properties.get("confidence", 1) < .5 or properties.get("status") in {"refuted", "withdrawn"}:
                continue
            a = (edge["owner_id"], edge["owner_version"], properties.get("source_statement_id", ""))
            b = (edge["target_id"], edge["target_version"], properties.get("target_statement_id", ""))
            if a in inactive or b in inactive:
                continue
            label = f"{edge['kind']}:{properties['predicate']}"
            if a[2] or b[2]:
                label += f"[{a[2]}->{b[2]}]"
            adjacency[a].append((b, label))
            undirected = properties.get("direction") == "undirected" or (
                edge["kind"] == "semantic" and properties["predicate"] == "related") or (
                edge["kind"] == "temporal" and properties["predicate"] in {"same_section", "near_time"})
            adjacency[b].append((a, label if undirected else "reverse:"+label))
            for node in (a, b):
                if node[2]:
                    members[node[:2]].add(node)
        return adjacency, members, entity_nodes

    def _walk_graph(self, seeds, adjacency, entity_nodes, links, record_keys, *, temporal=False):
        node_entities = defaultdict(set)
        for link in links:
            node = (link["id"], link["version"], link["statement_id"])
            if node in entity_nodes[link["entity_id"]]:
                node_entities[node].add(link["entity_id"])
        # Equal-hop entity hits precede generic semantic seeds; each record gets one shortest path.
        queue = [(0, 0 if path[0].startswith("entity:") else 1, node, path) for node, path in seeds.items()]
        heapq.heapify(queue)
        seen, paths = set(), {}
        while queue and len(seen) < self.graph_nodes:
            depth, priority, node, path = heapq.heappop(queue)
            if node[:2] in record_keys and (temporal or depth or path[0].startswith("entity:")):
                paths.setdefault(record_keys[node[:2]], path)
            if node in seen:
                continue
            seen.add(node)
            if depth >= self.graph_depth:
                continue
            neighbors = list(adjacency[node])
            if node[2]:
                # A package-level relation applies to this fact too; do not jump to its other statements.
                neighbors.extend(adjacency[(*node[:2], "")])
            if temporal:
                neighbors = [(other, label) for other, label in neighbors if label.removeprefix("reverse:").startswith(
                    ("causal:", "temporal:", "semantic:support", "semantic:context"))]
            else:
                for eid in sorted(node_entities[node]):
                    neighbors.extend((other, f"shared_entity:{eid}") for other in sorted(entity_nodes[eid])[:self.candidate_limit] if other != node)
            for other, label in neighbors[:self.graph_nodes]:
                if other not in seen:
                    heapq.heappush(queue, (depth+1, priority, other, [*path, self._node_label(node), label]))
        return paths

    @staticmethod
    def infer_temporal(query, paths=()):
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", query+" "+" ".join(paths))
        if match:
            volume, chapter, section = map(int, match.groups())
            lo = f"{volume}.{chapter}.{section}"
            hi = f"{volume}.{chapter if chapter else 999999}.{section if section else 999999}"
            return {"dimension": "chapter", "start": lo, "end": hi, "hard": False}
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", query)
        if dates:
            return {"dimension": "occurred", "start": dates[0], "end": (dates[-1]+"T23:59:59.999999"), "hard": False}
        return None
