"""evaluate.py — RAGAS evaluation routes for DocuMind.

Endpoints:
    POST /api/evaluate/single — evaluate a single Q&A pair
    POST /api/evaluate/batch  — evaluate a batch of Q&A pairs
"""

import asyncio
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from src.api.dependencies import get_current_user, get_eval_manager
from src.api.limiter import limiter
from src.components.evalution import EvaluationManager

router = APIRouter(prefix="/api/evaluate", tags=["evaluation"])


# ──────────────────────────────────────────────────────────────────────────────
#  Request / Response models
# ──────────────────────────────────────────────────────────────────────────────


class SingleEvalRequest(BaseModel):
    question: str
    answer: str
    contexts: List[str]
    ground_truth: Optional[str] = None


class BatchEvalRequest(BaseModel):
    test_set: List[Dict]


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/single", status_code=status.HTTP_200_OK)
@limiter.limit("5/minute")
async def evaluate_single(
    request: Request,
    payload: SingleEvalRequest,
    current_user: dict = Depends(get_current_user),
    eval_mgr: EvaluationManager = Depends(get_eval_manager),
):
    """Evaluate a single Q&A pair using RAGAS metrics.

    Returns faithfulness, answer relevancy, context precision, and
    (if ground_truth is provided) context recall.
    """
    # BUG-15 fix: evaluate_single is sync and makes several real LLM calls
    # under the hood (RAGAS) — calling it directly here would block
    # FastAPI's event loop for the whole evaluation. asyncio.to_thread
    # runs it in a worker thread instead.
    scores = await asyncio.to_thread(
        eval_mgr.evaluate_single,
        query=payload.question,
        answer=payload.answer,
        contexts=payload.contexts,
        ground_truth=payload.ground_truth,
    )
    return {"scores": scores}


@router.post("/batch", status_code=status.HTTP_200_OK)
@limiter.limit("2/minute")
async def evaluate_batch(
    request: Request,
    payload: BatchEvalRequest,
    current_user: dict = Depends(get_current_user),
    eval_mgr: EvaluationManager = Depends(get_eval_manager),
):
    """Evaluate a batch of Q&A pairs using RAGAS metrics.

    Each item in test_set should have keys: question, answer, contexts,
    and optionally ground_truth.

    Returns per-row scores and aggregated summary.
    """
    # BUG-15 fix: same reasoning as evaluate_single, and batch scales the
    # blocking cost with len(test_set) — a tighter rate limit than single
    # reflects that it's the more expensive of the two.
    result = await asyncio.to_thread(eval_mgr.evaluate_batch, payload.test_set)
    return result

