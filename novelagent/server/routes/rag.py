"""Project reference-library API."""

import asyncio
import uuid
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from novelagent.rag.store import RagStore

router = APIRouter(prefix="/api")


class ImportDocumentRequest(BaseModel):
    title: str = Field(default="未命名文章", max_length=200)
    source_name: str = Field(default="", max_length=200)
    encoding: str = Field(default="utf-8", max_length=40)
    content: str = Field(min_length=1)


@router.post("/rag/documents")
async def import_document(body: ImportDocumentRequest, request: Request):
    try:
        document = await request.app.state.rag_store.add_document(body.title, body.content, body.source_name, body.encoding)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    jobs = getattr(request.app.state, "rag_embedding_jobs", {})
    job_id = f"rag_emb_{uuid.uuid4().hex}"
    jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "completed_chunks": 0,
        "total_chunks": document["chunk_count"],
        "phase": "loading",
        "error": "",
    }
    request.app.state.rag_embedding_jobs = jobs

    async def run() -> None:
        job = jobs[job_id]

        def update(completed: int, total: int) -> None:
            job.update({"completed_chunks": completed, "total_chunks": total, "phase": "embedding"})

        try:
            await request.app.state.rag_store.load_embedding_model()
            job["phase"] = "embedding"
            result = await request.app.state.rag_store.embed_document(document["document_id"], update)
            job.update({"status": "completed", "completed_chunks": result["embedded_chunks"], "total_chunks": result["total_chunks"], "phase": "completed"})
        except RuntimeError as exc:
            job.update({"status": "failed", "error": str(exc)})
        except Exception as exc:
            job.update({"status": "failed", "error": f"向量构建失败：{exc}"})

    asyncio.create_task(run())
    return {**document, "embedding_job": jobs[job_id]}


@router.get("/rag/documents")
async def list_documents(request: Request):
    return await request.app.state.rag_store.list_documents()


@router.post("/rag/embeddings/rebuild")
async def rebuild_embeddings(request: Request):
    """Explicitly backfill vectors for documents imported before Emb was enabled."""
    jobs = getattr(request.app.state, "rag_embedding_jobs", {})
    job_id = f"rag_emb_{uuid.uuid4().hex}"
    jobs[job_id] = {"job_id": job_id, "status": "running", "completed_chunks": 0, "total_chunks": 0, "phase": "loading", "error": ""}
    request.app.state.rag_embedding_jobs = jobs

    async def run() -> None:
        job = jobs[job_id]
        def update(completed: int, total: int) -> None:
            job.update({"completed_chunks": completed, "total_chunks": total, "phase": "embedding"})
        try:
            await request.app.state.rag_store.load_embedding_model()
            job["phase"] = "embedding"
            result = await request.app.state.rag_store.rebuild_embeddings(update)
            job.update({"status": "completed", "completed_chunks": result["embedded_chunks"], "total_chunks": result["missing_chunks"], "phase": "completed"})
        except RuntimeError as exc:
            job.update({"status": "failed", "error": str(exc)})

    asyncio.create_task(run())
    return jobs[job_id]


@router.get("/rag/embeddings/rebuild/{job_id}")
async def get_rebuild_status(job_id: str, request: Request):
    job = getattr(request.app.state, "rag_embedding_jobs", {}).get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="向量构建任务不存在或服务已重启")
    return job


@router.delete("/rag/documents/{document_id}")
async def delete_document(document_id: str, request: Request):
    if not await request.app.state.rag_store.delete_document(document_id):
        raise HTTPException(status_code=404, detail="资料不存在")
    return {"status": "deleted"}


@router.get("/rag/documents/{document_id}/chunks")
async def get_document_chunks(document_id: str, request: Request, offset: int = 0, limit: int = 50):
    page = await request.app.state.rag_store.get_document_chunks(
        document_id,
        max(0, offset),
        max(1, min(limit, 100)),
    )
    if not page:
        raise HTTPException(status_code=404, detail="资料不存在")
    return page


@router.get("/rag/search")
async def search_documents(request: Request, q: str = "", limit: int = 5):
    return await request.app.state.rag_store.search(q, limit)
