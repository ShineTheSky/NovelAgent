"""Trace replay APIs and explicit historical-import action."""

import json
from fastapi import APIRouter, HTTPException, Request
from pathlib import Path

from novelagent.memory.file_store import FileStore
from novelagent.trace.store import TraceStore


router = APIRouter(prefix="/api")


def _store(request: Request) -> TraceStore:
    return request.app.state.trace_store


@router.post("/sessions/{session_id}/traces/import-history")
async def import_session_history_as_traces(session_id: str, request: Request):
    """Explicit, idempotent legacy import. Imported traces never create memories by themselves."""
    from novelagent.storage import models

    session = await models.load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        messages = json.loads(session.messages_json or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"会话消息格式无效: {exc.msg}") from exc
    if not isinstance(messages, list):
        raise HTTPException(status_code=422, detail="会话消息不是列表格式")
    return await _store(request).import_historical_session(session_id, session.project_id, messages)


@router.get("/sessions/{session_id}/traces")
async def list_session_traces(session_id: str, request: Request):
    """List executions for a session without returning their potentially large event payloads."""
    return await _store(request).list_session_traces(session_id)


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """Return one immutable trace and its ordered, nested events for replay/analysis."""
    trace = await _store(request).get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    trace["events"] = await _store(request).list_events(trace_id, limit=500)
    trace["classification"] = await _store(request).get_trace_classification(trace_id)
    return trace


@router.get("/projects/{project_id}/memory-patterns")
async def list_memory_patterns(project_id: str, request: Request):
    """Expose reviewed and pending cross-memory patterns for future profile UI."""
    return await _store(request).list_patterns(project_id, limit=100)


@router.get("/projects/{project_id}/traces")
async def list_project_traces(project_id: str, request: Request):
    """List the project's immutable trace evidence records."""
    return await _store(request).list_project_traces(project_id, limit=100)


@router.get("/projects/{project_id}/trace-memories")
async def list_trace_memories(project_id: str, request: Request):
    """Return trace-backed atomic memories for the project-insights panel."""
    return await _store(request).list_memories(project_id, limit=150)


@router.get("/projects/{project_id}/rules")
async def list_project_rules(project_id: str, request: Request):
    """Return active project rules that may be injected into agent context."""
    return await _store(request).list_rules(project_id, limit=100)


@router.get("/projects/{project_id}/trace-memories/{memory_id}")
async def get_trace_memory(project_id: str, memory_id: str, request: Request):
    """Return a single atomic memory with its project-local Markdown body."""
    memory = await _store(request).get_memory(project_id, memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    file_path = memory.get("file_path", "")
    if file_path.startswith(".memory/"):
        project_dir = Path(request.app.state.agent_loop.working_dir) / project_id
        memory["content"] = FileStore(str(project_dir)).read(file_path.removeprefix(".memory/"))
    else:
        memory["content"] = memory["claim"]
    return memory


@router.post("/projects/{project_id}/trace-memories/{memory_id}/downgrade")
async def downgrade_trace_memory(project_id: str, memory_id: str, request: Request):
    """User-confirmed lifecycle downgrade: Rule → Memory → Trace evidence."""
    result = await _store(request).downgrade_memory(project_id, memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail="记忆不存在或已降为 Trace")
    memory = result.get("memory") or await _store(request).get_memory(project_id, memory_id)
    if memory:
        path = request.app.state.trace_memory_materializer.sync(memory)
        await _store(request).set_memory_file_path(memory_id, path)
    return result
