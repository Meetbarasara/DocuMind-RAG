"""evaluate.py — RAGAS evaluation routes for DocuMind.

Endpoints:
    POST /api/evaluate/single — evaluate a single Q&A pair
    POST /api/evaluate/batch  — evaluate a batch of Q&A pairs
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from src.api.dependencies import get_config, get_current_user
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
#  Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_eval_manager: Optional[EvaluationManager] = None


def _get_eval_manager() -> EvaluationManager:
    """Return (and cache) the global EvaluationManager instance."""
    global _eval_manager
    if _eval_manager is None:
        _eval_manager = EvaluationManager(get_config())
    return _eval_manager


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/single", status_code=status.HTTP_200_OK)
async def evaluate_single(
    payload: SingleEvalRequest,
    current_user: dict = Depends(get_current_user),
):
    """Evaluate a single Q&A pair using RAGAS metrics.

    Returns faithfulness, answer relevancy, context precision, and
    (if ground_truth is provided) context recall.
    """
    eval_mgr = _get_eval_manager()
    scores = eval_mgr.evaluate_single(
        query=payload.question,
        answer=payload.answer,
        contexts=payload.contexts,
        ground_truth=payload.ground_truth,
    )
    return {"scores": scores}


@router.post("/batch", status_code=status.HTTP_200_OK)
async def evaluate_batch(
    payload: BatchEvalRequest,
    current_user: dict = Depends(get_current_user),
):
    """Evaluate a batch of Q&A pairs using RAGAS metrics.

    Each item in test_set should have keys: question, answer, contexts,
    and optionally ground_truth.

    Returns per-row scores and aggregated summary.
    """
    eval_mgr = _get_eval_manager()
    result = eval_mgr.evaluate_batch(payload.test_set)
    return result
