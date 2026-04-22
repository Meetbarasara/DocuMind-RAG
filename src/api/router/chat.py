"""chat.py — Chat (RAG query) routes for DocuMind.

Endpoints:
    POST /api/chat/query        — blocking Q&A
    POST /api/chat/query/stream — streaming Q&A (SSE)
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.dependencies import get_current_user, get_pipeline
from src.pipeline.pipeline import RAGPipeline

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ──────────────────────────────────────────────────────────────────────────────
#  Request / Response models
# ──────────────────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str          # "human" or "ai"
    content: str


class ChatRequest(BaseModel):
    question: str
    chat_history: Optional[List[ChatMessage]] = []
    filename_filter: Optional[str] = None   # restrict retrieval to one file


class ChatResponse(BaseModel):
    answer: str
    sources: List[dict]
    rewritten_query: str
    num_sources_used: int
    namespace: str


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/query", response_model=ChatResponse)
async def query(
    payload: ChatRequest,
    current_user: dict = Depends(get_current_user),
    pipeline: RAGPipeline = Depends(get_pipeline),
):
    """Answer a question using the RAG pipeline (blocking)."""
    user_id = str(current_user["user"].id)

    history = [
        {"role": msg.role, "content": msg.content}
        for msg in (payload.chat_history or [])
    ]

    try:
        result = pipeline.query(
            question=payload.question,
            namespace=user_id,
            chat_history=history,
            filename_filter=payload.filename_filter,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RAG query failed: {e}",
        )

    return ChatResponse(
        answer=result["answer"],
        sources=result["sources"],
        rewritten_query=result.get("rewritten_query", payload.question),
        num_sources_used=result["num_sources_used"],
        namespace=result.get("namespace", user_id),
    )


@router.post("/query/stream")
async def query_stream(
    payload: ChatRequest,
    current_user: dict = Depends(get_current_user),
    pipeline: RAGPipeline = Depends(get_pipeline),
):
    """Answer a question using the RAG pipeline with SSE streaming."""
    user_id = str(current_user["user"].id)

    history = [
        {"role": msg.role, "content": msg.content}
        for msg in (payload.chat_history or [])
    ]

    def event_generator():
        yield from pipeline.query_stream(
            question=payload.question,
            namespace=user_id,
            chat_history=history,
            filename_filter=payload.filename_filter,
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
