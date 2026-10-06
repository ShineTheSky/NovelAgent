"""Trace replay APIs and explicit historical-import action."""

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.trace.store import TraceStore
from novelagent.history import DocumentHistoryStore
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.migration import KnowledgeMigration, preview as migration_preview
from novelagent.memory.history_input import HistoryBodyReferences


router = APIRouter(prefix="/api")


def _store(request: Request) -> TraceStore:
    return request.app.state.trace_store


def _files(request: Request, project_id: str) -> FileLifecycleStore:
    return FileLifecycleStore(request.app.state.agent_loop.working_dir, project_id)


def _history(request: Request, project_id: str) -> DocumentHistoryStore:
    workspace = Path(request.app.state.agent_loop.working_dir).resolve()
    project_dir = (workspace / project_id).resolve()
    if not project_dir.is_relative_to(workspace):
        raise HTTPException(status_code=403, detail="项目路径越界")
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="项目不存在")
    return DocumentHistoryStore(project_dir)


@router.get("/projects/{project_id}/history/files")
async def list_project_history_files(project_id: str, request: Request,
                                     limit: int = Query(25, ge=1, le=100),
                                     offset: int = Query(0, ge=0)):
    return _history(request, project_id).list_files(limit=limit, offset=offset)


@router.get("/projects/{project_id}/history")
async def list_project_history(project_id: str, request: Request, kind: str = "", path: str = "",
                               limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    if kind not in {"", "document", "conversation", "requirement", "review"}:
        raise HTTPException(status_code=422, detail="无效的 History 类型")
    if path and kind != "document":
        raise HTTPException(status_code=422, detail="文件路径仅适用于文件 History")
    return _history(request, project_id).list_units(kind=kind, path=path,
                                                     limit=limit, offset=offset)


@router.get("/projects/{project_id}/history/{history_id}")
async def get_project_history(project_id: str, history_id: str, request: Request, body_references: bool = False):
    history = _history(request, project_id)
    unit = history.get_unit(history_id)
    if unit is None or unit["kind"] not in {"document", "conversation", "requirement", "review"}:
        raise HTTPException(status_code=404, detail="History 不存在")
    if body_references:
        bodies = HistoryBodyReferences(history)
        for key in ("events", "archived_events", "previous_attempt_events"):
            unit[key] = bodies.display_events(unit.get(key, []))
        unit["body_versions"] = list(bodies.catalog.values())
    return unit


@router.post("/projects/{project_id}/traces/analyze-pending")
@router.post("/projects/{project_id}/history/analyze-pending")
async def analyze_pending_history(project_id: str, request: Request):
    """Legacy endpoint: schedule pending History extraction, not Trace memories."""
    workspace = Path(request.app.state.agent_loop.working_dir).resolve()
    project_dir = (workspace / project_id).resolve()
    if not project_dir.is_relative_to(workspace):
        raise HTTPException(status_code=403, detail="项目路径越界")
    history = DocumentHistoryStore(project_dir)
    analyzer = request.app.state.agent_loop.history_analyzer
    pending = history.list_analyzable(include_partial=bool(getattr(analyzer, "knowledge_pipeline", None)))
    for history_id in pending:
        asyncio.create_task(analyzer.analyze(project_id, history_id))
    return {"scheduled": len(pending)}


@router.get("/projects/{project_id}/insights")
async def list_insights(project_id: str, request: Request):
    return _knowledge_records(request, project_id, "insight")


@router.get("/projects/{project_id}/memories")
async def list_file_memories(project_id: str, request: Request):
    return _knowledge_records(request, project_id, "memory")


@router.get("/projects/{project_id}/patterns")
async def list_patterns(project_id: str, request: Request):
    return _knowledge_records(request, project_id, "pattern")


def _knowledge_records(request, project_id, layer):
    history = _history(request, project_id)
    store = KnowledgeStore(history.project_dir)
    fresh = [store.view(r) for r in store.list((layer,), include_inactive=True)]
    legacy = [{**r, "schema": "legacy"} for r in _files(request, project_id).list(layer)]
    return fresh + legacy


@router.get("/projects/{project_id}/knowledge/{record_id}")
async def get_knowledge_record(project_id: str, record_id: str, request: Request,
                               version: int | None = Query(None, ge=1)):
    store = KnowledgeStore(_history(request, project_id).project_dir)
    record = store.get(record_id, version)
    if not record:
        raise HTTPException(status_code=404, detail="知识记录或版本不存在")
    return store.view(record)


@router.get("/projects/{project_id}/knowledge/{record_id}/facts/{fact_id}")
async def get_fact_history(project_id: str, record_id: str, fact_id: str, request: Request):
    store = KnowledgeStore(_history(request, project_id).project_dir)
    record = store.get(record_id)
    if not record or record["layer"] != "memory":
        raise HTTPException(status_code=404, detail="事实包不存在")
    history = store.fact_history(record_id, fact_id)
    if not history["versions"] and not history["changes"]:
        raise HTTPException(status_code=404, detail="事实历史不存在")
    return history


class MigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    history_ids: list[str] = Field(min_length=1, max_length=5)
    confirm_model_calls: bool = False


@router.get("/projects/{project_id}/knowledge-migration")
async def preview_knowledge_migration(project_id: str, request: Request):
    return migration_preview(_history(request, project_id).project_dir)


@router.post("/projects/{project_id}/knowledge-migration")
async def run_knowledge_migration(project_id: str, body: MigrationRequest, request: Request):
    _history(request, project_id)
    analyzer = request.app.state.agent_loop.history_analyzer
    if not getattr(analyzer, "knowledge_pipeline", None):
        raise HTTPException(status_code=409, detail="新知识流程尚未启用")
    if not body.confirm_model_calls:
        raise HTTPException(status_code=422, detail="迁移会调用模型；请先预览来源并明确确认模型调用")
    migration = KnowledgeMigration(analyzer)
    try:
        ids = migration.validate(project_id, body.history_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Persist authorization before scheduling, so a restart cannot lose accepted work.
    for hid in ids:
        migration.pipeline.store(project_id).migration_progress(hid, {"status": "running"})
    request.app.state.agent_trace_tasks.submit(migration.run(project_id, ids), label="knowledge-migration")
    return {"scheduled": ids, "progress_url": f"/api/projects/{project_id}/knowledge-migration"}


@router.post("/projects/{project_id}/knowledge/retry-pending")
async def retry_knowledge(project_id: str, request: Request):
    _history(request, project_id)
    runtime = getattr(request.app.state, "knowledge_runtime", None)
    if not runtime:
        raise HTTPException(status_code=409, detail="新知识流程尚未启用")
    pending = runtime.pipeline.store(project_id).jobs()
    request.app.state.agent_trace_tasks.submit(runtime.pipeline.consolidate_pending(project_id, retry_failed=True), label="knowledge-retry")
    return {"scheduled": len(pending)}


@router.post("/projects/{project_id}/{layer}/{record_id}/downgrade")
async def downgrade_file_record(project_id: str, layer: str, record_id: str, request: Request):
    if layer not in {"memory", "pattern"}:
        raise HTTPException(status_code=422, detail="只能降低 Memory 或 Pattern")
    if KnowledgeStore(_history(request, project_id).project_dir).get(record_id):
        raise HTTPException(status_code=409, detail="版本化知识不能通过旧层级降级接口修改")
    result = _files(request, project_id).downgrade(layer, record_id)
    if not result:
        raise HTTPException(status_code=404, detail="记录不存在")
    return result


@router.post("/projects/{project_id}/{layer}/{record_id}/cancel-manual-review")
async def cancel_file_record_manual_review(project_id: str, layer: str, record_id: str, request: Request):
    if layer not in {"insight", "memory"}:
        raise HTTPException(status_code=422, detail="只能取消 Insight 或 Memory 的手动降级标注")
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
