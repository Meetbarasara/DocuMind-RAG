"""conversations.py — persistent chat history (Claude-style conversations).

Endpoints (all require auth; everything is scoped to the calling user):
    POST   /api/conversations                       — start a new conversation
    GET    /api/conversations                       — list the user's conversations
    GET    /api/conversations/{id}/messages         — load a conversation's messages
    POST   /api/conversations/{id}/messages         — append a message
    DELETE /api/conversations/{id}                  — delete a conversation
"""

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.api.dependencies import get_current_user, get_db
from src.api.error_utils import log_and_get_ref
from src.api.limiter import limiter
from src.components.database import SupabaseManager
from src.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# ──────────────────────────────────────────────────────────────────────────────
#  Request models
# ──────────────────────────────────────────────────────────────────────────────


class CreateConversationRequest(BaseModel):
    title: Optional[str] = None


class AddMessageRequest(BaseModel):
    role: str                       # "human" | "ai"
    content: str
    sources: Optional[List[dict]] = None
    run_id: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
@limiter.limit("60/minute")
async def create_conversation(
    request: Request,
    payload: CreateConversationRequest,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Start a new (empty) conversation."""
    user_id = str(current_user["user"].id)
    try:
        conv = await asyncio.to_thread(db.create_conversation, user_id, payload.title or "New chat")
    except Exception as e:
        ref = log_and_get_ref(logger, "create_conversation failed", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not create conversation. (ref: {ref})",
        )
    return conv


@router.get("")
async def list_conversations(
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """List the user's conversations, most-recently-updated first."""
    user_id = str(current_user["user"].id)
    convos = await asyncio.to_thread(db.list_conversations, user_id)
    return {"conversations": convos}


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Return a conversation's messages (scoped to the calling user)."""
    user_id = str(current_user["user"].id)
    messages = await asyncio.to_thread(db.get_conversation_messages, user_id, conversation_id)
    return {"messages": messages}


@router.post("/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
@limiter.limit("120/minute")
async def add_message(
    request: Request,
    conversation_id: str,
    payload: AddMessageRequest,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Append one message (human or ai) to a conversation."""
    user_id = str(current_user["user"].id)
    try:
        msg = await asyncio.to_thread(
            db.add_message,
            user_id,
            conversation_id,
            payload.role,
            payload.content,
            payload.sources,
            payload.run_id,
        )
    except Exception as e:
        ref = log_and_get_ref(logger, "add_message failed", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not save message. (ref: {ref})",
        )
    return msg


@router.delete("/{conversation_id}", status_code=status.HTTP_200_OK)
async def delete_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Delete a conversation (its messages cascade)."""
    user_id = str(current_user["user"].id)
    ok = await asyncio.to_thread(db.delete_conversation, user_id, conversation_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not delete conversation.",
        )
    return {"message": "Conversation deleted"}
