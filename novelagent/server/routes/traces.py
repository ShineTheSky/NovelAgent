"""Trace replay APIs and explicit historical-import action."""

import asyncio
import json
from fastapi import APIRouter, HTTPException, Query, Request

from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.trace.store import TraceStore


router = APIRouter(prefix="/api")


def _store(request: Request) -> TraceStore:
    return request.app.state.trace_store


def _files(request: Request, project_id: str) -> FileLifecycleStore:
    return FileLifecycleStore(request.app.state.agent_loop.working_dir, project_id)


@router.post("/projects/{project_id}/traces/analyze-pending")
async def analyze_pending_traces(project_id: str, request: Request):
    """Schedule historical/pending Trace extraction without blocking the UI."""
    traces = await _store(request).list_project_traces(project_id, limit=500)
    pending = [trace for trace in traces if trace.get("analysis_status") == "pending"]
    analyzer = request.app.state.agent_loop.post_turn_analyzer
    for trace in pending:
        asyncio.create_task(analyzer.analyze(trace["trace_id"], project_id))
    return {"scheduled": len(pending)}


@router.get("/projects/{project_id}/evidence")
async def list_evidence(project_id: str, request: Request):
    return _files(request, project_id).list("evidence")


@router.get("/projects/{project_id}/memories")
async def list_file_memories(project_id: str, request: Request):
    return _files(request, project_id).list("memory")


@router.get("/projects/{project_id}/patterns")
async def list_patterns(project_id: str, request: Request):
    return _files(request, project_id).list("pattern")


@router.post("/projects/{project_id}/{layer}/{record_id}/downgrade")
async def downgrade_file_record(project_id: str, layer: str, record_id: str, request: Request):
    if layer not in {"memory", "pattern"}:
        raise HTTPException(status_code=422, detail="只能降低 Memory 或 Pattern")
    result = _files(request, project_id).downgrade(layer, record_id)
    if not result:
        raise HTTPException(status_code=404, detail="记录不存在")
    return result


@router.post("/projects/{project_id}/{layer}/{record_id}/cancel-manual-review")
async def cancel_file_record_manual_review(project_id: str, layer: str, record_id: str, request: Request):
    if layer not in {"evidence", "memory"}:
        raise HTTPException(status_code=422, detail="只能取消 Evidence 或 Memory 的手动降级标注")
    result = _files(request, project_id).cancel_manual_review(layer, record_id)
    if not result:
        raise HTTPException(status_code=404, detail="记录不存在")
    return result


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
async def get_trace(trace_id: str, request: Request, event_limit: int = Query(500, ge=1, le=500),
                    event_offset: int = Query(0, ge=0)):
    """Return one immutable trace and its ordered, nested events for replay/analysis."""
    trace = await _store(request).get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    trace["events"] = await _store(request).list_events(
        trace_id, limit=event_limit, offset=event_offset,
    )
    trace["event_count"] = await _store(request).count_events(trace_id)
    trace["event_offset"] = event_offset
    trace["classification"] = await _store(request).get_trace_classification(trace_id)
    return trace


@router.get("/traces/{trace_id}/validation")
async def validate_trace(trace_id: str, request: Request):
    result = await _store(request).validate_trace_chain(trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    return result


@router.get("/traces/{trace_id}/context")
async def get_trace_context(trace_id: str, request: Request, before: int = 2, after: int = 2):
    """Return the bounded request chain needed to reconstruct a Trace."""
    context = await _store(request).get_trace_context(
        trace_id, before=max(0, min(before, 10)), after=max(0, min(after, 10)),
    )
    if context is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    return context


@router.get("/projects/{project_id}/traces")
async def list_project_traces(project_id: str, request: Request):
    """List the project's immutable trace evidence records."""
    return await _store(request).list_project_traces(project_id, limit=100)


@router.get("/projects/{project_id}/trace-management")
async def manage_project_traces(
    project_id: str, request: Request, q: str = "", session_id: str = "",
    status: str = "", operation_kind: str = "", analysis_status: str = "", source_agent: str = "",
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
):
    """Read-only, filtered Trace inventory for the management panel."""
    return await _store(request).list_project_traces_page(
        project_id, search=q.strip(), session_id=session_id.strip(),
        status=status.strip(), operation_kind=operation_kind.strip(),
        analysis_status=analysis_status.strip(), source_agent=source_agent.strip(),
        limit=limit, offset=offset,
    )


@router.get("/projects/{project_id}/trace-agent-stats")
async def trace_agent_stats(project_id: str, request: Request):
    return await _store(request).get_project_trace_agent_stats(project_id)
