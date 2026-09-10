"""Project reference-library API."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from novelagent.rag.store import RagStore

router = APIRouter(prefix="/api")


class ImportDocumentRequest(BaseModel):
    title: str = Field(default="未命名文章", max_length=200)
    source_name: str = Field(default="", max_length=200)
    content: str = Field(min_length=1)


@router.post("/projects/{project_id}/rag/documents")
async def import_document(project_id: str, body: ImportDocumentRequest, request: Request):
    try:
        return await request.app.state.rag_store.add_document(project_id, body.title, body.content, body.source_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/rag/documents")
async def list_documents(project_id: str, request: Request):
    return await request.app.state.rag_store.list_documents(project_id)


@router.delete("/projects/{project_id}/rag/documents/{document_id}")
async def delete_document(project_id: str, document_id: str, request: Request):
    if not await request.app.state.rag_store.delete_document(project_id, document_id):
        raise HTTPException(status_code=404, detail="资料不存在")
    return {"status": "deleted"}


@router.get("/projects/{project_id}/rag/search")
async def search_documents(project_id: str, request: Request, q: str = "", limit: int = 5):
    return await request.app.state.rag_store.search(project_id, q, limit)
