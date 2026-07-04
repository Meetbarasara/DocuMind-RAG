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
from src.components.compliance import (
    Requirement,
    diff_requirements,
    run_check,
    summarize,
    summarize_rows,
)
from src.components.database import SupabaseManager
from src.components.judge import JudgeNotConfigured, build_judge_llm
from src.logger import get_logger
from src.pipeline.pipeline import RAGPipeline

logger = get_logger(__name__)

router = APIRouter(prefix="/api/compliance", tags=["compliance"])


class ComplianceCheckRequest(BaseModel):
    regulation_id: str
    policy_label: Optional[str] = None      # human label stored on the result


class RecheckRequest(BaseModel):
    check_id: str                           # a prior check to re-evaluate against the updated circular


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
    origin; the policy citation is the judge's evidence quote grounded back to a
    specific policy *clause* (see compliance._verify_evidence) — never a
    model-invented page. ``policy_clause`` is the verbatim source sentence (what
    the UI shows side-by-side); ``evidence_verified`` says the quote actually
    grounded in a retrieved clause.
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
        "policy_clause": verdict.policy_clause,
        "policy_filename": verdict.policy_filename,
        "policy_page": _to_int(verdict.policy_page),
        "evidence_score": verdict.evidence_score,
        "evidence_verified": verdict.policy_filename is not None,
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


def _rows_to_requirements(rows: List[dict]) -> List[Requirement]:
    """Reconstruct the OLD requirement list from a prior check's rows — each row
    records the requirement text it was judged against. This is the 'old' side of
    the change-tracking diff (against the regulation's CURRENT requirements)."""
    out: List[Requirement] = []
    for r in rows or []:
        text = (r.get("requirement") or "").strip()
        if not text:
            continue
        out.append(Requirement(
            id=r.get("requirement_id") or f"req-{len(out) + 1}",
            text=text,
            page=r.get("rbi_page"),
            section=r.get("rbi_section"),
        ))
    return out


def _carried_row(prior_row: dict, new_req: Requirement) -> dict:
    """A row for an UNCHANGED requirement: reuse the prior verdict verbatim (no
    re-judge — that's the whole point of change-tracking), re-keyed to the new
    version's id / page / section so the table matches the current circular."""
    row = dict(prior_row)
    row["requirement_id"] = new_req.id
    row["requirement"] = new_req.text
    row["rbi_page"] = _to_int(new_req.page)
    row["rbi_section"] = new_req.section
    row["change"] = "unchanged"
    row["carried_forward"] = True
    return row


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


@router.post("/recheck")
@limiter.limit("10/minute")
async def recheck(
    request: Request,
    payload: RecheckRequest,
    current_user: dict = Depends(get_current_user),
    pipeline: RAGPipeline = Depends(get_pipeline),
    db: SupabaseManager = Depends(get_db),
):
    """Re-check a prior gap check against the CURRENT version of its regulation.

    Only requirements **added or changed** since that check are re-judged; the
    verdicts for **unchanged** requirements are carried forward (no LLM call), and
    requirements **removed** from the new circular are reported and dropped. This
    turns an updated 34-requirement circular from a ~15-minute full re-run into a
    few judge calls. Streams like /check, plus a per-event ``delta`` block and a
    ``change`` / ``carried_forward`` tag on each row.
    """
    user_id = str(current_user["user"].id)

    # Validate up front (before the 200 stream begins), like /check.
    prior = await asyncio.to_thread(db.get_compliance_check, user_id, payload.check_id)
    if not prior:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prior check not found.")

    regulation = await asyncio.to_thread(db.get_regulation, prior.get("regulation_id"))
    if not regulation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The regulation for this check no longer exists.",
        )

    new_reqs = _load_requirements(regulation)
    if not new_reqs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This regulation has no extracted requirements yet — run the seed step.",
        )

    old_reqs = _rows_to_requirements(prior.get("rows") or [])
    diff = diff_requirements(old_reqs, new_reqs)
    delta = diff.counts()

    # Only the added + changed requirements need the judge; unchanged ones carry
    # forward. If nothing changed we never build the judge (so a delta-free
    # re-check still works even if the judge key is absent).
    to_judge = list(diff.added) + [new for _old, new in diff.changed]
    change_of = {n.id: "added" for n in diff.added}
    change_of.update({n.id: "changed" for _old, n in diff.changed})
    prior_by_id = {r.get("requirement_id"): r for r in (prior.get("rows") or [])}

    judge_llm = None
    retrieval_manager = None
    if to_judge:
        try:
            judge_llm = build_judge_llm(pipeline.config)
        except JudgeNotConfigured as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Compliance judge is not configured: {e}",
            )
        retrieval_manager = pipeline._get_retrieval_manager(user_id)

    reg_meta = {"id": regulation.get("id"), "name": regulation.get("name"),
                "regulator": regulation.get("regulator")}
    total = len(diff.unchanged) + len(to_judge)     # rows in the new table (removed aren't rows)

    async def event_generator():
        yield _sse({"type": "summary_init", "total": total, "regulation": reg_meta, "delta": delta})

        rows: List[dict] = []
        # Unchanged first — instant, no judge call.
        for old_req, new_req in diff.unchanged:
            prior_row = prior_by_id.get(old_req.id)
            if prior_row is None:
                continue                            # defensive: old_reqs came FROM these rows
            row = _carried_row(prior_row, new_req)
            rows.append(row)
            yield _sse({"type": "row", "checked": len(rows), "total": total, "row": row})

        # Added + changed — re-judged against the user's current policy.
        if to_judge:
            try:
                async for req, verdict in run_check(to_judge, retrieval_manager, judge_llm):
                    row = _row(req, verdict)
                    row["change"] = change_of.get(req.id, "changed")
                    row["carried_forward"] = False
                    rows.append(row)
                    yield _sse({"type": "row", "checked": len(rows), "total": total, "row": row})
            except Exception as e:
                ref = log_and_get_ref(logger, "compliance recheck failed mid-stream", e)
                yield _sse({"type": "error",
                            "message": f"The re-check failed partway through. (ref: {ref})"})

        rows.sort(key=lambda r: _req_sort_key(r["requirement_id"]))
        summary = summarize_rows(rows)

        check_id = None
        try:
            saved = await asyncio.to_thread(
                db.save_compliance_check, user_id,
                f"{prior.get('policy_label') or regulation.get('name') or 'Policy check'} · re-check",
                regulation.get("id"), {**summary, "delta": delta}, rows,
            )
            check_id = (saved or {}).get("id")
        except Exception as e:
            log_and_get_ref(logger, "compliance recheck persist failed", e)

        yield _sse({"type": "summary_final", "summary": summary, "delta": delta, "check_id": check_id})
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
