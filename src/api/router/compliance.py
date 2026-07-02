"""compliance.py — KYC gap-analysis routes.

Endpoints (all require auth; the policy namespace is always the calling user, so
one company can never see another's policy):
    GET  /api/compliance/regulations   — list regulations available to check against
    POST /api/compliance/check         — run a gap check, streamed row-by-row (SSE)
    GET  /api/compliance/checks        — list the user's past checks
    GET  /api/compliance/checks/{id}   — fetch one past check (the full cited gap table)

The heavy lifting lives in src/components/compliance.py (the engine) and judge.py
(the judge model). This module is just the HTTP wiring: validate, stream, persist.
"""

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.error_utils import log_and_get_ref
from src.api.limiter import limiter
from src.components.compliance import Requirement, run_check, summarize
from src.components.database import SupabaseManager
from src.components.judge import JudgeNotConfigured, build_judge_llm
from src.logger import get_logger
from src.pipeline.pipeline import RAGPipeline

logger = get_logger(__name__)

router = APIRouter(prefix="/api/compliance", tags=["compliance"])


class ComplianceCheckRequest(BaseModel):
    regulation_id: str
    policy_label: Optional[str] = None      # human label stored on the result


# ── Serialisation helpers ────────────────────────────────────────────────────

def _to_int(v):
    """Pinecone returns numeric metadata as float (page 1 → 1.0). Normalise page
    numbers back to int so citations read 'p.2', not 'p.2.0'."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _req_sort_key(req_id: str) -> int:
    """Order rows by requirement number ('req-7' → 7). run_check yields in
    completion order, so we sort before persisting for a stable table."""
    try:
        return int(str(req_id).split("-")[-1])
    except (ValueError, IndexError):
        return 1_000_000


def _row(req: Requirement, verdict) -> dict:
    """Serialise one (requirement, verdict) into a gap-table row.

    The RBI reference (rbi_page/section) is carried from the requirement's
    origin; the policy citation (filename/page) is the judge's evidence quote
    matched back to a real chunk (see compliance.py) — never a model-invented page.
    """
    return {
        "requirement_id": req.id,
        "requirement": req.text,
        "rbi_page": _to_int(req.page),
        "rbi_section": req.section,
        "status": verdict.status,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "policy_quote": verdict.policy_quote,
        "policy_filename": verdict.policy_filename,
        "policy_page": _to_int(verdict.policy_page),
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _load_requirements(regulation: dict) -> List[Requirement]:
    """Rebuild Requirement objects from a regulation's cached JSON list."""
    out: List[Requirement] = []
    for r in regulation.get("requirements") or []:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        out.append(Requirement(
            id=r.get("id") or f"req-{len(out) + 1}",
            text=text,
            page=r.get("page"),
            section=r.get("section"),
        ))
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/regulations")
async def list_regulations(
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """List regulations available to check against (shared reference data)."""
    regs = await asyncio.to_thread(db.list_regulations)
    return {"regulations": regs}


@router.post("/check")
@limiter.limit("10/minute")
async def check(
    request: Request,
    payload: ComplianceCheckRequest,
    current_user: dict = Depends(get_current_user),
    pipeline: RAGPipeline = Depends(get_pipeline),
    db: SupabaseManager = Depends(get_db),
):
    """Run a requirement-by-requirement gap check of the user's policy docs
    against a regulation, streaming each cited row (SSE) as it is judged."""
    user_id = str(current_user["user"].id)

    # Validate up front: once we return a StreamingResponse the HTTP status is
    # already 200 and can't be changed, so all the failable checks happen here.
    regulation = await asyncio.to_thread(db.get_regulation, payload.regulation_id)
    if not regulation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Regulation not found.")

    requirements = _load_requirements(regulation)
    if not requirements:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This regulation has no extracted requirements yet — run the seed step.",
        )

    try:
        judge_llm = build_judge_llm(pipeline.config)
    except JudgeNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Compliance judge is not configured: {e}",
        )

    # The policy lives in the user's own namespace; retrieval is bound to it, so
    # a check only ever sees this user's policy docs (regulations are separate).
    retrieval_manager = pipeline._get_retrieval_manager(user_id)
    reg_meta = {"id": regulation.get("id"), "name": regulation.get("name"),
                "regulator": regulation.get("regulator")}

    async def event_generator():
        yield _sse({"type": "summary_init", "total": len(requirements), "regulation": reg_meta})

        verdicts = []
        try:
            async for req, verdict in run_check(requirements, retrieval_manager, judge_llm):
                verdicts.append((req, verdict))
                yield _sse({"type": "row", "checked": len(verdicts),
                            "total": len(requirements), "row": _row(req, verdict)})
        except Exception as e:
            # A mid-stream failure can't change the 200 already sent — surface it
            # as an SSE error event (with a log ref) rather than a broken stream.
            ref = log_and_get_ref(logger, "compliance check failed mid-stream", e)
            yield _sse({"type": "error",
                        "message": f"The check failed partway through. (ref: {ref})"})

        verdicts.sort(key=lambda rv: _req_sort_key(rv[0].id))
        rows = [_row(req, v) for req, v in verdicts]
        summary = summarize([v for _, v in verdicts])

        # Persist so re-opening the check is instant and never re-burns judge
        # budget. Best-effort — a storage hiccup must not fail a finished check.
        check_id = None
        try:
            saved = await asyncio.to_thread(
                db.save_compliance_check, user_id,
                payload.policy_label or regulation.get("name") or "Policy check",
                regulation.get("id"), summary, rows,
            )
            check_id = (saved or {}).get("id")
        except Exception as e:
            log_and_get_ref(logger, "compliance check persist failed", e)

        yield _sse({"type": "summary_final", "summary": summary, "check_id": check_id})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/checks")
async def list_checks(
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """List the user's past checks, newest first (summary counts only)."""
    user_id = str(current_user["user"].id)
    checks = await asyncio.to_thread(db.list_compliance_checks, user_id)
    return {"checks": checks}


@router.get("/checks/{check_id}")
async def get_check(
    check_id: str,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Fetch one past check incl. its full gap table, scoped to the caller."""
    user_id = str(current_user["user"].id)
    check = await asyncio.to_thread(db.get_compliance_check, user_id, check_id)
    if not check:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Check not found.")
    return check
