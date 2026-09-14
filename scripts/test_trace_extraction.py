"""Run one pending Trace through the configured file-backed extractor."""

import asyncio
import argparse
import sqlite3
from pathlib import Path

import yaml

from novelagent.llm.client import LLMClient
from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.trace.embedding_gate import EmbeddingGate
from novelagent.trace.store import TraceStore


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-id")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect("data/novelagent.db")
    if args.list:
        rows = connection.execute(
            "SELECT trace_id, project_id, analysis_status, user_message FROM traces ORDER BY started_at"
        ).fetchall()
        connection.close()
        for trace_id, project_id, status, user_message in rows:
            print({"trace_id": trace_id, "project_id": project_id, "status": status, "user_message": user_message})
        return
    query = "SELECT trace_id, project_id FROM traces WHERE analysis_status = 'pending'"
    params: tuple[str, ...] = ()
    if args.trace_id:
        query = "SELECT trace_id, project_id FROM traces WHERE trace_id = ?"
        params = (args.trace_id,)
    rows = connection.execute(query + " ORDER BY started_at" + ("" if args.all else " LIMIT 1"), params).fetchall()
    connection.close()
    if not rows:
        print({"status": "no_pending_trace"})
        return

    store = TraceStore()
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8")) or {}
    embedding = config.get("embedding", {})
    gate = EmbeddingGate(
        embedding.get("model_path", "models/bge-base-zh-v1.5"),
        enabled=embedding.get("enabled", True),
        routine_min_similarity=float(embedding.get("routine_min_similarity", 0.70)),
        routine_min_margin=float(embedding.get("routine_min_margin", 0.12)),
    )
    analyzer = FileTraceAnalyzer(LLMClient("config/llm_config.yaml"), "workspace", store, gate)
    summary = {"complete": 0, "skipped": 0, "feedback": 0, "evidence": 0, "memory": 0}
    for trace_id, project_id in rows:
        await analyzer.analyze(trace_id, project_id)
        trace = await store.get_trace(trace_id)
        classification = await store.get_trace_classification(trace_id) or {}
        summary[trace.get("analysis_status", "complete")] = summary.get(trace.get("analysis_status", "complete"), 0) + 1
        summary["feedback"] += int(bool(classification.get("has_feedback")))
        files = FileLifecycleStore("workspace", project_id)
        evidence = [item for item in files.list("evidence") if item.get("trace_id") == trace_id]
        memory = [item for item in files.list("memory") if item.get("trace_id") == trace_id]
        summary["evidence"] += len(evidence)
        summary["memory"] += len(memory)
        print({"trace_id": trace_id, "status": trace.get("analysis_status"), "evidence": len(evidence), "memory": len(memory)})
    print({"total": len(rows), **summary})


if __name__ == "__main__":
    asyncio.run(main())
