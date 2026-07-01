"""compliance.py — the KYC gap-analysis engine (core, LLM-provider-agnostic).

Two steps, both driven by the *judge* LLM (see judge.py):

  1. extract_requirements() — turn a regulation into atomic, checkable
     requirements, each tagged with the page it came from.
  2. judge_requirement()    — decide whether the company's policy satisfies ONE
     requirement, using ONLY the retrieved policy excerpts.

Anti-hallucination design:
  - The judge returns an evidence *quote*; we derive the citation (filename+page)
    by MATCHING that quote back to a real policy chunk — we never trust a
    page number the model made up.
  - JSON is parsed defensively: a malformed/uncertain response degrades to
    status "Needs review", it never crashes the whole check.
"""

import json
import re
from dataclasses import dataclass
from typing import List, Optional

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from src.logger import get_logger

logger = get_logger(__name__)

VALID_STATUSES = ("Covered", "Partial", "Gap", "Conflict")
NEEDS_REVIEW = "Needs review"


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class Requirement:
    id: str
    text: str
    page: Optional[int] = None          # RBI source page (citation origin)
    section: Optional[str] = None


@dataclass
class Verdict:
    requirement_id: str
    status: str                         # one of VALID_STATUSES or "Needs review"
    rationale: str = ""
    confidence: float = 0.0
    policy_quote: str = ""
    policy_filename: Optional[str] = None   # derived by matching, not trusted
    policy_page: Optional[int] = None


# ── Prompts ────────────────────────────────────────────────────────────────

_EXTRACT_SYS = """\
You extract atomic compliance requirements from a regulatory passage.
Return ONLY JSON: {"requirements": [{"text": "...", "section": "..."}]}.
Each requirement is ONE self-contained obligation, phrased as what a regulated
entity must do. Use only what the passage states — do not invent requirements.
If the passage contains no obligation, return {"requirements": []}."""

_JUDGE_SYS = """\
You are a compliance analyst. Decide whether the company's policy satisfies ONE
regulatory requirement, using ONLY the provided policy excerpts.
Return ONLY JSON:
{"status": "...", "policy_quote": "...", "confidence": 0.0, "rationale": "..."}
- status is EXACTLY one of: Covered, Partial, Gap, Conflict.
  Covered = policy fully meets it. Partial = addressed but incomplete.
  Gap = not addressed at all. Conflict = policy contradicts the requirement.
- policy_quote = the exact sentence from the excerpts that is your evidence,
  copied verbatim. Empty string if there is no relevant excerpt (a Gap).
- confidence = 0.0-1.0. rationale = one short sentence.
Do not use outside knowledge. Do not invent a quote that is not in the excerpts."""


# ── JSON robustness ────────────────────────────────────────────────────────

def _parse_json_object(text: str) -> dict:
    """Best-effort parse of the first JSON object in *text*.

    Handles: clean JSON, ```json fences, and prose wrapped around a {...} block.
    Raises ValueError if nothing parseable is found.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", s, re.DOTALL)
    if match:
        return json.loads(match.group(0))  # may still raise -> caller handles
    raise ValueError("no JSON object found in model output")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _match_quote_to_chunk(quote: str, chunks: List[Document]):
    """Find the policy chunk a quote came from → (filename, page), or (None, None).

    Whitespace-normalized substring match; falls back to the quote's first 40
    chars (handles the model trimming punctuation). This is how we cite the
    POLICY evidence without trusting the model's page number.
    """
    q = _norm(quote)
    if not q:
        return None, None
    probes = [q] if len(q) <= 40 else [q, q[:40]]
    for probe in probes:
        for ch in chunks:
            if probe in _norm(ch.page_content):
                return ch.metadata.get("filename"), ch.metadata.get("page_number")
    return None, None


# ── Step 1: requirement extraction ─────────────────────────────────────────

async def extract_requirements(chunks: List[Document], judge_llm) -> List[Requirement]:
    """Extract atomic requirements from regulation *chunks* (map over chunks).

    Each chunk is processed independently (avoids context overflow on a long
    circular); each extracted requirement records the page it came from so the
    RBI citation is carried from origin, not asked of the judge later.
    """
    out: List[Requirement] = []
    for ch in chunks:
        page = ch.metadata.get("page_number")
        try:
            resp = await judge_llm.ainvoke([
                SystemMessage(content=_EXTRACT_SYS),
                HumanMessage(content=ch.page_content),
            ])
            data = _parse_json_object(resp.content)
            reqs = data.get("requirements", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.warning("Requirement extraction failed on a chunk (page=%s): %s", page, e)
            continue
        for r in reqs:
            text = (r.get("text") or "").strip() if isinstance(r, dict) else ""
            if not text:
                continue
            out.append(Requirement(
                id=f"req-{len(out) + 1}",
                text=text,
                page=page,
                section=(r.get("section") or None) if isinstance(r, dict) else None,
            ))
    logger.info("Extracted %d requirements from %d regulation chunks", len(out), len(chunks))
    return out


# ── Step 2: the judge ──────────────────────────────────────────────────────

def _build_judge_messages(requirement: Requirement, policy_chunks: List[Document]):
    excerpts = "\n\n".join(
        f"[{i + 1}] (from {c.metadata.get('filename', '?')}, "
        f"page {c.metadata.get('page_number', '?')})\n{c.page_content}"
        for i, c in enumerate(policy_chunks)
    ) or "(no policy excerpts found)"
    human = f"Requirement:\n{requirement.text}\n\nCompany policy excerpts:\n{excerpts}"
    return [SystemMessage(content=_JUDGE_SYS), HumanMessage(content=human)]


async def judge_requirement(
    requirement: Requirement, policy_chunks: List[Document], judge_llm
) -> Verdict:
    """Judge whether the policy satisfies *requirement*. Never raises."""
    try:
        resp = await judge_llm.ainvoke(_build_judge_messages(requirement, policy_chunks))
        data = _parse_json_object(resp.content)
    except Exception as e:
        logger.warning("Judge failed on %s: %s", requirement.id, e)
        return Verdict(requirement_id=requirement.id, status=NEEDS_REVIEW,
                       rationale="Could not parse a verdict — needs human review.")

    status = str(data.get("status", "")).strip().title()
    if status not in VALID_STATUSES:
        status = NEEDS_REVIEW

    quote = (data.get("policy_quote") or "").strip()
    filename, page = _match_quote_to_chunk(quote, policy_chunks)
    # A cited quote that matches NO retrieved chunk is a hallucination signal:
    # keep it visible but flag for review rather than presenting it as evidence.
    if quote and filename is None and status in ("Covered", "Partial"):
        status = NEEDS_REVIEW

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return Verdict(
        requirement_id=requirement.id,
        status=status,
        rationale=str(data.get("rationale", "")).strip(),
        confidence=confidence,
        policy_quote=quote,
        policy_filename=filename,
        policy_page=page,
    )
