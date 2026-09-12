"""Trace replay plus file-backed Evidence, Memory and Pattern APIs."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from novelagent.memory.memory_manager import MemoryManager
from novelagent.trace.store import TraceStore


router = APIRouter(prefix="/api")


class PatternReviewRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|disputed)$")
    confidence: float = Field(ge=0, le=1)


def _store(request: Request) -> TraceStore:
    return request.app.state.trace_store


def _manager(request: Request, project_id: str) -> MemoryManager:
    working_dir = Path(request.app.state.agent_loop.working_dir).resolve()
    project_dir = (working_dir / project_id).resolve()
    if working_dir not in project_dir.parents:
        raise HTTPException(status_code=404, detail="项目不存在")
    return MemoryManager(str(project_dir))


def _record(record: dict, layer: str, project_id: str) -> dict:
    identifier = {"evidence": "evidence_id", "memory": "memory_id", "pattern": "pattern_id"}[layer]
    result = {
        identifier: str(record.get(identifier, "")),
        "project_id": project_id,
        "trace_id": str(record.get("trace_id", "")),
        "source_event_ids": [str(value) for value in record.get("source_event_ids", [])],
        "kind": str(record.get("kind") or record.get("type", "")),
        "subtype": str(record.get("subtype", "")),
        "claim": str(record.get("claim") or record.get("summary", "")),
        "scope": str(record.get("scope", "project")),
        "confidence": float(record.get("confidence", 0.5)),
        "status": str(record.get("status", "")),
        "file_path": str(record.get("_path", "")),
        "created_at": str(record.get("created", "")),
        "updated_at": str(record.get("updated", "")),
    }
    if layer == "pattern":
        result.update({
            "pattern_id": str(record.get("pattern_id", "")),
            "dimension": str(record.get("dimension", "")),
            "supporting_memory_ids": [str(value) for value in record.get("supporting_memory_ids", [])],
            "contradicting_memory_ids": [str(value) for value in record.get("contradicting_memory_ids", [])],
            "trace_ids": [str(value) for value in record.get("trace_ids", [])],
            "support_count": int(record.get("support_count", 0)),
            "contradiction_count": int(record.get("contradiction_count", 0)),
        })
    return result


def _list_records(request: Request, project_id: str, layer: str) -> list[dict]:
    manager = _manager(request, project_id)
    return [_record(record, layer, project_id) for record in manager.list_records(layer)]


def _detail(request: Request, project_id: str, layer: str, record_id: str) -> dict:
    manager = _manager(request, project_id)
    record = manager.get_record(layer, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    result = _record(record, layer, project_id)
    result["content"] = manager.file_store.read(result["file_path"].removeprefix(".memory/"))
    return result


@router.get("/sessions/{session_id}/traces")
async def list_session_traces(session_id: str, request: Request):
    return await _store(request).list_session_traces(session_id)


@router.get("/projects/{project_id}/traces")
async def list_project_traces(project_id: str, request: Request):
    # The project check prevents turning this endpoint into an arbitrary file namespace.
    _manager(request, project_id)
    from novelagent.storage.database import get_connection
    conn = await get_connection()
    cursor = await conn.execute(
        "SELECT * FROM traces WHERE project_id = ? ORDER BY started_at DESC LIMIT 100", (project_id,)
    )
    traces = [dict(row) for row in await cursor.fetchall()]
    await conn.close()
    return traces


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request, offset: int = 0, limit: int = 500):
    trace = await _store(request).get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    trace["events"] = await _store(request).list_events(trace_id, limit=max(1, min(limit, 1000)), offset=max(0, offset))
    trace["classification"] = await _store(request).get_trace_classification(trace_id)
    return trace


@router.get("/projects/{project_id}/traces/{trace_id}/window")
async def get_trace_window(project_id: str, trace_id: str, event_id: list[str], request: Request):
    if not await _store(request).get_project_trace(project_id, trace_id):
        raise HTTPException(status_code=404, detail="Trace 不存在")
    return await _store(request).get_trace_event_window(trace_id, event_id)


@router.get("/projects/{project_id}/evidence")
async def list_evidence(project_id: str, request: Request):
    return _list_records(request, project_id, "evidence")[:150]


@router.get("/projects/{project_id}/evidence/{evidence_id}")
async def get_evidence(project_id: str, evidence_id: str, request: Request):
    return _detail(request, project_id, "evidence", evidence_id)


@router.get("/projects/{project_id}/memories")
async def list_memories(project_id: str, request: Request):
    return _list_records(request, project_id, "memory")[:150]


@router.get("/projects/{project_id}/memories/{memory_id}")
async def get_memory(project_id: str, memory_id: str, request: Request):
    return _detail(request, project_id, "memory", memory_id)


@router.get("/projects/{project_id}/memory-patterns")
async def list_memory_patterns(project_id: str, request: Request):
    return _list_records(request, project_id, "pattern")[:100]


@router.get("/projects/{project_id}/memory-patterns/{pattern_id}")
async def get_memory_pattern(project_id: str, pattern_id: str, request: Request):
    return _detail(request, project_id, "pattern", pattern_id)


@router.patch("/projects/{project_id}/memory-patterns/{pattern_id}")
async def review_memory_pattern(project_id: str, pattern_id: str, body: PatternReviewRequest, request: Request):
    manager = _manager(request, project_id)
    pattern = manager.get_record("pattern", pattern_id)
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern 不存在")
    pattern["status"] = body.status
    pattern["confidence"] = body.confidence
    manager.create_or_update_pattern({
        "pattern_id": pattern_id,
        "kind": pattern.get("kind", "preference"),
        "subtype": pattern.get("subtype", ""),
        "dimension": pattern.get("dimension", ""),
        "claim": pattern.get("claim") or pattern.get("summary", ""),
        "scope": pattern.get("scope", "project"),
        "confidence": body.confidence,
        "summary": pattern.get("summary", ""),
        "memory_links": [
            *({"memory_id": value, "relation": "support"} for value in pattern.get("supporting_memory_ids", [])),
            *({"memory_id": value, "relation": "contradict"} for value in pattern.get("contradicting_memory_ids", [])),
        ],
        "trace_ids": pattern.get("trace_ids", []),
        "status": body.status,
    })
    return _detail(request, project_id, "pattern", pattern_id)


# Compatibility endpoints retained for existing clients.
@router.get("/projects/{project_id}/trace-memories")
async def list_trace_memories(project_id: str, request: Request):
    return await list_memories(project_id, request)


@router.get("/projects/{project_id}/trace-memories/{memory_id}")
async def get_trace_memory(project_id: str, memory_id: str, request: Request):
    return await get_memory(project_id, memory_id, request)
