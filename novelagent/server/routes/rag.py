"""Project reference-library API."""

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
        return await request.app.state.rag_store.add_document(body.title, body.content, body.source_name, body.encoding)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rag/documents")
async def list_documents(request: Request):
    return await request.app.state.rag_store.list_documents()


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
