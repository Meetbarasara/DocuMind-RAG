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

import asyncio
import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Tuple

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from src.logger import get_logger

logger = get_logger(__name__)

VALID_STATUSES = ("Covered", "Partial", "Gap", "Conflict")
NEEDS_REVIEW = "Needs review"

# How much of the judge's evidence quote must be found in a single policy clause
# for the citation to count as verified. High enough that an ungrounded quote is
# rejected, loose enough to tolerate benign reformatting (a fixed typo, dropped
# article, normalised punctuation). Tunable; measured by the compliance eval's
# evidence-faithfulness metric.
EVIDENCE_MATCH_THRESHOLD = 0.8


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
    policy_quote: str = ""                  # the judge's evidence quote (as emitted)
    policy_clause: str = ""                 # the verbatim source clause it grounds to
    policy_filename: Optional[str] = None   # derived by matching, not trusted
    policy_page: Optional[int] = None
    evidence_score: float = 0.0             # 0-1: how well the quote grounds (see _verify_evidence)


# ── Prompts ────────────────────────────────────────────────────────────────

_EXTRACT_SYS = """\
You extract atomic compliance requirements from a regulatory passage.
Return ONLY JSON: {"requirements": [{"text": "...", "section": "..."}]}.
Each requirement is ONE self-contained obligation, phrased as what a regulated
entity must do. Use only what the passage states — do not invent requirements.
If the passage contains no obligation, return {"requirements": []}."""

# The Conflict definition spells out that a *weaker stated value* (e.g. a shorter
# retention period than required) is a Conflict, not Partial. The compliance eval
# (scripts/run_compliance_eval) showed the judge otherwise softens a 3yr-vs-5yr
# retention contradiction into Partial — which under-states a real violation, the
# worst error direction in compliance.
_JUDGE_SYS = """\
You are a compliance analyst. Decide whether the company's policy satisfies ONE
regulatory requirement, using ONLY the provided policy excerpts.
Return ONLY JSON:
{"status": "...", "policy_quote": "...", "confidence": 0.0, "rationale": "..."}
- status is EXACTLY one of: Covered, Partial, Gap, Conflict.
  Covered  = the policy fully meets the requirement.
  Partial  = the policy addresses the requirement but is incomplete, vague, or
             discretionary, or is silent on some conditions - without stating a
             concrete rule that violates it.
  Gap      = the requirement is not addressed at all in the excerpts.
  Conflict = the policy states a CONCRETE rule or value that directly contradicts
             the requirement - e.g. it mandates a 3-year retention where 5 years
             is required. A concrete policy value that is less strict than a
             concrete required minimum is a Conflict. Mere vagueness, discretion,
             or silence is Partial or Gap, NOT Conflict.
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


# Split policy text after sentence/clause punctuation or a newline. A retrieved
# chunk is ~512 tokens = many clauses; we cite the ONE clause the evidence grounds
# to, not the whole blob.
_CLAUSE_SPLIT = re.compile(r"(?<=[.;:])\s+|\n+")
_MIN_CLAUSE_LEN = 12                          # drop fragments too short to ground a real quote


def _split_clauses(text: str) -> List[str]:
    """Break policy text into clause-sized spans for precise citation.

    Splits on sentence/clause boundaries; drops tiny fragments. If nothing splits
    out (a short chunk), the whole text is one clause — so short excerpts still
    work exactly as before.
    """
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(text or "") if p and p.strip()]
    clauses = [p for p in parts if len(p) >= _MIN_CLAUSE_LEN]
    return clauses or ([text.strip()] if (text or "").strip() else [])


def _containment_score(needle: str, haystack: str) -> float:
    """How much of *needle* is present, contiguously, inside *haystack* (0.0-1.0).

    Sum of difflib matching-block sizes over ``len(needle)`` — a verbatim quote
    scores 1.0, a lightly-edited one scores high, an unrelated one near 0.
    Normalised by the quote length (not by both strings, as SequenceMatcher.ratio
    does) so a short quote fully inside a long clause still scores 1.0: the
    question is "is the quote in the clause?", not "are the two similar in size?".
    """
    if not needle:
        return 0.0
    matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(needle)


def _verify_evidence(quote: str, chunks: List[Document]):
    """Locate the specific policy CLAUSE a quote grounds to and score the match.

    Returns ``(clause, filename, page, score)``:
      - clause   : the verbatim source clause (the precise citation span), or "".
      - filename : the chunk that clause is in, or None.
      - page     : that chunk's page, or None.
      - score    : 0.0-1.0 grounding score (best clause's containment of the quote).

    This replaces a whole-chunk substring test and improves it two ways:
      1. It pinpoints the exact clause, so the citation (and the side-by-side UI)
         shows the real policy sentence, not a 512-token blob or the model's
         paraphrase.
      2. A graded score tolerates benign reformatting that an exact-substring match
         wrongly rejected as unverified, while a quote grounded in nothing scores
         near zero and stays flagged.
    filename/page/clause are returned ONLY when the score clears
    EVIDENCE_MATCH_THRESHOLD — an ungrounded quote never yields a citation. The
    raw score is always returned (for the eval + review), even below threshold.
    """
    q = _norm(quote)
    if not q:
        return "", None, None, 0.0
    best_clause, best_fn, best_pg, best_score = "", None, None, 0.0
    for ch in chunks:
        for clause in _split_clauses(ch.page_content):
            score = _containment_score(q, _norm(clause))
            if score > best_score:
                best_clause = clause
                best_fn = ch.metadata.get("filename")
                best_pg = ch.metadata.get("page_number")
                best_score = score
    if best_score >= EVIDENCE_MATCH_THRESHOLD:
        return best_clause, best_fn, best_pg, best_score
    return "", None, None, best_score


# ── Step 1: requirement extraction ─────────────────────────────────────────

async def extract_requirements(chunks: List[Document], judge_llm) -> List[Requirement]:
    """Extract atomic requirements from regulation *chunks* (map over chunks).

    Each chunk is processed independently (avoids context overflow on a long
    circular); each extracted requirement records the page it came from so the
    RBI citation is carried from origin, not asked of the judge later.
    """
    out: List[Requirement] = []
    seen: set = set()                       # normalized texts already kept (dedup below)
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
            # Dedup verbatim re-extractions: a long (real) regulation page splits
            # into overlapping chunks (64-token overlap), so a requirement near a
            # boundary can be extracted twice. Keep the first (earliest page).
            key = _norm(text)
            if key in seen:
                continue
            seen.add(key)
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
    clause, filename, page, score = _verify_evidence(quote, policy_chunks)
    # A cited quote that grounds in NO retrieved clause is a hallucination signal:
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
        policy_clause=clause,
        policy_filename=filename,
        policy_page=page,
        evidence_score=round(score, 3),
    )


# ── Remediation suggestion (grounded in the requirement, on demand) ─────────

_SUGGEST_SYS = """You are a compliance analyst helping a fintech close ONE specific gap between its internal KYC policy and an RBI requirement.

You are given the RBI requirement, the current status of the company's policy against it, and the company's current clause (if any).

Write a concise DRAFT policy clause the company can add or amend to satisfy the requirement.
Rules:
- Address ONLY this requirement. Do not introduce obligations beyond it.
- If a current clause is given and it is partial or conflicts (e.g. a shorter retention period than required), REVISE that clause to comply and state the corrected value explicitly.
- Write it as policy text in the third person ("The company shall ...").
- Keep it to 2-4 sentences.
- Do NOT cite RBI section numbers or invent any legal citation.
- Output only the draft clause — no preamble, no headings, no markdown."""


async def suggest_remediation(
    requirement_text: str,
    status: str,
    policy_clause: str,
    rationale: str,
    llm,
) -> str:
    """Draft a policy clause to close one gap, grounded in the RBI requirement.

    Generative but bounded: the model rewrites/adds a clause to satisfy the given
    requirement — it never introduces obligations beyond it or invents legal
    citations. Returned as a draft for human review (the UI labels it so). Unlike
    the check path (which degrades a bad row to "Needs review"), this raises on
    LLM failure so the route can surface a clear error to the one waiting user.
    """
    parts = [f"RBI requirement:\n{requirement_text.strip()}", f"Current status: {status}"]
    if policy_clause.strip():
        parts.append(f"Company's current clause:\n{policy_clause.strip()}")
    else:
        parts.append("Company's current clause: (none — the policy is silent on this)")
    if rationale.strip():
        parts.append(f"Why it falls short:\n{rationale.strip()}")
    messages = [SystemMessage(content=_SUGGEST_SYS), HumanMessage(content="\n\n".join(parts))]
    resp = await llm.ainvoke(messages)
    return (resp.content or "").strip()


# ── Orchestration: run a full check (streamed, bounded concurrency) ─────────

async def run_check(requirements: List[Requirement], retrieval_manager, judge_llm, *,
                    concurrency: int = 3):
    """Async-generate ``(Requirement, Verdict)`` per requirement, as each finishes.

    Retrieval is per-requirement and blocking, so it runs in a thread. At most
    ``concurrency`` requirements are judged at once — a bounded pool so a
    rate-limited free judge tier doesn't 429-storm (the exact failure mode we hit
    running RAGAS). Yields in completion order; the caller sorts/persists.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(req: Requirement):
        async with sem:
            docs = await asyncio.to_thread(retrieval_manager.retrieve, req.text)
            return req, await judge_requirement(req, docs, judge_llm)

    tasks = [asyncio.create_task(_one(r)) for r in requirements]
    try:
        for fut in asyncio.as_completed(tasks):
            yield await fut
    finally:
        for t in tasks:                 # consumer stopped early -> don't leak tasks
            if not t.done():
                t.cancel()


def summarize(verdicts: List[Verdict]) -> dict:
    """Count verdicts by status for the summary cards."""
    return summarize_rows([{"status": v.status} for v in verdicts])


def summarize_rows(rows: List[dict]) -> dict:
    """Status counts from serialised gap-table rows — used by change-tracking,
    where carried-forward rows are plain dicts (a reused prior verdict), not
    Verdict objects."""
    counts = Counter(r.get("status") for r in rows)
    out = {"total": len(rows)}
    for s in (*VALID_STATUSES, NEEDS_REVIEW):
        out[s] = counts.get(s, 0)
    return out


# ── Change-tracking: diff two regulation versions ───────────────────────────
#
# When a circular is updated we re-check only what changed, not all N
# requirements (a full real-RBI check is ~34 judge calls = ~15 min on the free
# Cerebras tier). Requirement ids are positional (assigned at extraction), so
# they are NOT stable across versions — we match by TEXT similarity instead.
# Bias: only near-identical text counts as "unchanged". An under-called change
# just gets re-checked (wasted work, safe); an over-called "unchanged" would
# skip re-checking a genuine change — the dangerous direction in compliance.

REQ_UNCHANGED_THRESHOLD = 0.97   # >= this similarity: the same requirement, unchanged
REQ_MATCH_THRESHOLD = 0.6        # >= this (but < unchanged): a modified version of an old req


@dataclass
class RequirementDiff:
    unchanged: List[Tuple[Requirement, Requirement]]   # (old, new): text near-identical
    changed: List[Tuple[Requirement, Requirement]]     # (old, new): same obligation, modified
    added: List[Requirement]                           # in the new version only
    removed: List[Requirement]                         # in the old version only

    def counts(self) -> dict:
        return {"unchanged": len(self.unchanged), "changed": len(self.changed),
                "added": len(self.added), "removed": len(self.removed)}


def _text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b), autojunk=False).ratio()


def diff_requirements(old: List[Requirement], new: List[Requirement]) -> RequirementDiff:
    """Diff two versions of a regulation's requirement list by text similarity.

    Each new requirement is matched to at most one old requirement, **globally
    best-first** (highest-similarity pairs claimed first) so a rewording pairs
    with its true predecessor rather than an incidental look-alike:
      - similarity >= REQ_UNCHANGED_THRESHOLD -> unchanged (carry the old verdict)
      - similarity >= REQ_MATCH_THRESHOLD     -> changed   (must be re-judged)
      - otherwise                             -> the new one is added / the old one removed
    Pure and deterministic (no LLM) — change detection is free and unit-testable.
    Output lists preserve the input order of the version they come from.
    """
    scored = [
        (_text_similarity(n.text, o.text), i, j)
        for i, n in enumerate(new)
        for j, o in enumerate(old)
    ]
    scored = [t for t in scored if t[0] >= REQ_MATCH_THRESHOLD]
    scored.sort(key=lambda t: t[0], reverse=True)

    match_of_new: dict = {}          # new index -> (old index, similarity)
    used_old: set = set()
    for sim, i, j in scored:
        if i in match_of_new or j in used_old:
            continue
        match_of_new[i] = (j, sim)
        used_old.add(j)

    unchanged, changed = [], []
    for i, n in enumerate(new):
        if i not in match_of_new:
            continue
        j, sim = match_of_new[i]
        (unchanged if sim >= REQ_UNCHANGED_THRESHOLD else changed).append((old[j], n))
    added = [n for i, n in enumerate(new) if i not in match_of_new]
    removed = [o for j, o in enumerate(old) if j not in used_old]
    return RequirementDiff(unchanged=unchanged, changed=changed, added=added, removed=removed)
