"""The gap-analysis engine: requirement extraction + the judge. The LLM is
mocked (canned JSON), so these pin the parsing/citation/robustness logic — the
part that must never crash a check or present a hallucinated citation."""

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.components.compliance import (
    EVIDENCE_MATCH_THRESHOLD,
    NEEDS_REVIEW,
    Requirement,
    Verdict,
    _parse_json_object,
    _split_clauses,
    _verify_evidence,
    diff_requirements,
    extract_requirements,
    judge_requirement,
    run_check,
    summarize,
)


class _FakeLLM:
    """Async chat model stub — returns canned .content (str or a fn of messages)."""

    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages):
        c = self._content(messages) if callable(self._content) else self._content
        return SimpleNamespace(content=c)


def _policy(text, page=7, filename="acme_kyc_policy.pdf"):
    return Document(page_content=text, metadata={"filename": filename, "page_number": page})


# ── JSON robustness ────────────────────────────────────────────────────────

def test_parse_plain_fenced_and_prose_wrapped():
    assert _parse_json_object('{"a": 1}')["a"] == 1
    assert _parse_json_object('```json\n{"a": 2}\n```')["a"] == 2
    assert _parse_json_object('Sure! Here it is: {"a": 3} — done.')["a"] == 3
    with pytest.raises(ValueError):
        _parse_json_object("no json here")


# ── Clause-level citation verification ──────────────────────────────────────

def test_split_clauses_breaks_on_sentence_boundaries():
    text = "Clause A about OVD. Clause B about retention for five years; extra. Short"
    clauses = _split_clauses(text)
    assert "Clause A about OVD." in clauses
    assert any("retention for five years" in c for c in clauses)
    assert "Short" not in clauses          # a short trailing fragment can't cite anything


def test_split_clauses_short_chunk_is_one_clause():
    assert _split_clauses("We check an OVD.") == ["We check an OVD."]
    assert _split_clauses("   ") == []


def test_verify_evidence_verbatim_quote_scores_full():
    chunk = _policy("We verify an OVD for every customer at onboarding.")
    clause, fn, pg, score = _verify_evidence(
        "We verify an OVD for every customer at onboarding.", [chunk])
    assert score == 1.0
    assert fn == "acme_kyc_policy.pdf" and pg == 7
    assert clause == "We verify an OVD for every customer at onboarding."


def test_verify_evidence_pinpoints_the_clause_not_the_whole_chunk():
    # A multi-clause chunk: the quote grounds to ONE sentence, and we cite that
    # sentence — not the 3-sentence blob (the whole point of clause-level).
    chunk = _policy(
        "Customers submit an OVD at onboarding. Records are retained for five years "
        "after closure. Risk is assessed for every customer."
    )
    clause, fn, pg, score = _verify_evidence(
        "Records are retained for five years after closure.", [chunk])
    assert score >= EVIDENCE_MATCH_THRESHOLD
    assert clause == "Records are retained for five years after closure."
    assert "OVD" not in clause and "Risk" not in clause          # not the neighbours


def test_verify_evidence_ungrounded_quote_scores_low_and_yields_no_citation():
    chunk = _policy("We check an OVD for every customer.")
    clause, fn, pg, score = _verify_evidence("We perform a retina scan of the iris.", [chunk])
    assert score < EVIDENCE_MATCH_THRESHOLD
    assert (clause, fn, pg) == ("", None, None)                  # a hallucination cites nothing


def test_verify_evidence_empty_quote_is_no_match():
    assert _verify_evidence("", [_policy("anything")]) == ("", None, None, 0.0)


def test_verify_evidence_picks_the_best_of_several_chunks():
    a = _policy("Unrelated onboarding text about photographs.", page=1, filename="a.pdf")
    b = _policy("KYC records are kept for five years after the account closes.", page=4, filename="b.pdf")
    clause, fn, pg, score = _verify_evidence(
        "records are kept for five years after the account closes", [a, b])
    assert fn == "b.pdf" and pg == 4                             # matched the right chunk
    assert score >= EVIDENCE_MATCH_THRESHOLD


# ── Requirement extraction ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_attaches_page_and_ids():
    llm = _FakeLLM('{"requirements": [{"text": "Verify OVD at onboarding", "section": "1"}]}')
    chunks = [Document(page_content="Section 1 ...", metadata={"page_number": 2})]
    reqs = await extract_requirements(chunks, llm)
    assert len(reqs) == 1
    assert reqs[0].text == "Verify OVD at onboarding"
    assert reqs[0].page == 2 and reqs[0].id == "req-1"


@pytest.mark.asyncio
async def test_extract_skips_bad_chunk_without_crashing():
    # first chunk returns garbage, second returns a valid requirement
    def content(messages):
        return "garbage" if "BAD" in messages[-1].content else '{"requirements":[{"text":"Do X"}]}'
    llm = _FakeLLM(content)
    chunks = [Document(page_content="BAD", metadata={"page_number": 1}),
              Document(page_content="GOOD", metadata={"page_number": 2})]
    reqs = await extract_requirements(chunks, llm)
    assert [r.text for r in reqs] == ["Do X"]  # bad chunk skipped, not fatal


@pytest.mark.asyncio
async def test_extract_dedups_verbatim_requirements_across_chunks():
    # A long page splits into overlapping chunks, so the same requirement can be
    # extracted from two of them — it must be kept once, not duplicated.
    llm = _FakeLLM('{"requirements":[{"text":"Retain KYC records for five years"}]}')
    chunks = [Document(page_content="chunk A", metadata={"page_number": 1}),
              Document(page_content="chunk B", metadata={"page_number": 2})]
    reqs = await extract_requirements(chunks, llm)
    assert [r.text for r in reqs] == ["Retain KYC records for five years"]
    assert reqs[0].page == 1                # kept the first occurrence


# ── The judge ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_covered_cites_by_matching_the_quote_to_a_chunk():
    quote = "We verify an OVD for every customer at onboarding."
    llm = _FakeLLM('{"status":"Covered","policy_quote":"%s","confidence":0.9,"rationale":"ok"}' % quote)
    chunk = _policy("Our rule: " + quote + " No exceptions.", page=3)
    v = await judge_requirement(Requirement(id="r1", text="Identify with OVD"), [chunk], llm)
    assert v.status == "Covered"
    assert v.policy_filename == "acme_kyc_policy.pdf" and v.policy_page == 3  # derived, not trusted


@pytest.mark.asyncio
async def test_gap_has_no_citation_and_is_not_downgraded():
    llm = _FakeLLM('{"status":"Gap","policy_quote":"","confidence":0.8,"rationale":"absent"}')
    v = await judge_requirement(Requirement(id="r2", text="File STRs with FIU-IND"), [_policy("unrelated")], llm)
    assert v.status == "Gap" and v.policy_page is None


@pytest.mark.asyncio
async def test_hallucinated_quote_on_covered_is_flagged_for_review():
    # model claims Covered + a quote that appears in NO retrieved chunk
    llm = _FakeLLM('{"status":"Covered","policy_quote":"We do a retina scan.","confidence":0.7,"rationale":"x"}')
    v = await judge_requirement(Requirement(id="r3", text="Biometric check"), [_policy("We check an OVD.")], llm)
    assert v.status == NEEDS_REVIEW  # can't verify the citation -> don't present it as evidence


@pytest.mark.asyncio
async def test_lightly_reformatted_faithful_quote_still_verifies():
    # The policy says "...records ARE retained for five years..."; the judge
    # quotes it faithfully but drops "are". An exact-substring match wrongly flags
    # this real evidence as unverifiable; clause-level grounding must accept it.
    chunk = _policy("Client records are retained for five years after the account is closed.")
    llm = _FakeLLM('{"status":"Covered","policy_quote":"Client records retained for five years after the account is closed.","confidence":0.9,"rationale":"ok"}')
    v = await judge_requirement(Requirement(id="r", text="Retain records five years"), [chunk], llm)
    assert v.status == "Covered"                       # NOT downgraded to Needs review
    assert v.policy_filename == "acme_kyc_policy.pdf"  # citation still derived
    assert v.evidence_score >= EVIDENCE_MATCH_THRESHOLD
    assert "five years" in v.policy_clause.lower()     # the verbatim source clause, surfaced


@pytest.mark.asyncio
async def test_malformed_json_becomes_needs_review_not_a_crash():
    llm = _FakeLLM("the model rambled and produced no json")
    v = await judge_requirement(Requirement(id="r4", text="anything"), [_policy("x")], llm)
    assert v.status == NEEDS_REVIEW


@pytest.mark.asyncio
async def test_invalid_status_becomes_needs_review():
    llm = _FakeLLM('{"status":"Maybe","policy_quote":"","confidence":0.5,"rationale":"?"}')
    v = await judge_requirement(Requirement(id="r5", text="anything"), [_policy("x")], llm)
    assert v.status == NEEDS_REVIEW


@pytest.mark.asyncio
async def test_confidence_clamped():
    llm = _FakeLLM('{"status":"Gap","policy_quote":"","confidence":5,"rationale":"x"}')
    v = await judge_requirement(Requirement(id="r6", text="anything"), [_policy("x")], llm)
    assert v.confidence == 1.0


# ── Orchestration ──────────────────────────────────────────────────────────

class _FakeRM:
    """Retrieval manager stub — retrieve() returns one policy chunk per call."""

    def retrieve(self, query, *a, **k):
        return [_policy("We verify an OVD for every customer at onboarding.", page=1)]


@pytest.mark.asyncio
async def test_run_check_streams_a_verdict_per_requirement():
    llm = _FakeLLM('{"status":"Covered","policy_quote":"We verify an OVD for every customer at onboarding.","confidence":0.9,"rationale":"ok"}')
    reqs = [Requirement(id=f"r{i}", text=f"req {i}") for i in range(5)]
    seen = [v async for (_, v) in run_check(reqs, _FakeRM(), llm, concurrency=2)]
    assert len(seen) == 5
    assert all(v.status == "Covered" and v.policy_page == 1 for v in seen)


def test_summarize_counts_by_status():
    vs = [Verdict("a", "Covered"), Verdict("b", "Covered"), Verdict("c", "Gap"), Verdict("d", NEEDS_REVIEW)]
    s = summarize(vs)
    assert s == {"total": 4, "Covered": 2, "Partial": 0, "Gap": 1, "Conflict": 0, "Needs review": 1}


# ── Change-tracking: diff_requirements ──────────────────────────────────────

def _req(rid, text):
    return Requirement(id=rid, text=text)


def test_diff_identical_lists_are_all_unchanged():
    old = [_req("o1", "Maintain records for five years after the account is closed."),
           _req("o2", "Categorise every customer as low, medium or high risk.")]
    new = [_req("n1", "Maintain records for five years after the account is closed."),
           _req("n2", "Categorise every customer as low, medium or high risk.")]
    d = diff_requirements(old, new)
    assert d.counts() == {"unchanged": 2, "changed": 0, "added": 0, "removed": 0}
    assert [o.id for o, n in d.unchanged] == ["o1", "o2"]        # input order preserved


def test_diff_detects_added_removed_and_changed():
    old = [_req("o1", "Maintain records of customer identity for five years after the account is closed."),
           _req("o2", "Categorise every customer as low, medium or high risk."),
           _req("o3", "Appoint a Principal Officer for KYC compliance.")]
    new = [_req("n1", "Maintain records of customer identity for five years after the account is closed."),  # == o1
           _req("n2", "Categorise every customer as low, medium, high or very-high risk."),                  # ~ o2
           _req("n3", "File Suspicious Transaction Reports with FIU-IND within seven days.")]                # new
    d = diff_requirements(old, new)
    assert [n.id for o, n in d.unchanged] == ["n1"]
    assert [(o.id, n.id) for o, n in d.changed] == [("o2", "n2")]
    assert [n.id for n in d.added] == ["n3"]
    assert [o.id for o in d.removed] == ["o3"]                   # dropped from the new circular


def test_diff_pairs_with_the_most_similar_predecessor():
    # nx is identical to ob and only loosely like oa -> it must pair with ob
    # (global best-first), leaving oa removed, not first-match to oa.
    old = [_req("oa", "Retain KYC records for five years."),
           _req("ob", "Retain KYC records for five years after the account is closed.")]
    new = [_req("nx", "Retain KYC records for five years after the account is closed.")]
    d = diff_requirements(old, new)
    assert d.unchanged == [(old[1], new[0])]                     # paired with ob, the identical one
    assert [o.id for o in d.removed] == ["oa"]
    assert d.changed == [] and d.added == []


def test_diff_reworded_retention_period_is_changed_not_unchanged():
    # A retention-period change must be re-judged, never carried forward.
    old = [_req("o", "Maintain records for five years after the account is closed.")]
    new = [_req("n", "Maintain records for eight years after the account is closed.")]
    d = diff_requirements(old, new)
    assert d.counts() == {"unchanged": 0, "changed": 1, "added": 0, "removed": 0}
    assert (old[0], new[0]) in d.changed


def test_diff_empty_old_all_added_empty_new_all_removed():
    a, b = _req("a", "Requirement A about OVD collection."), _req("b", "Requirement B about risk assessment.")
    assert diff_requirements([], [a, b]).counts()["added"] == 2
    assert diff_requirements([a, b], []).counts()["removed"] == 2
