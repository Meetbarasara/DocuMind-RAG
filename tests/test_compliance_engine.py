"""The gap-analysis engine: requirement extraction + the judge. The LLM is
mocked (canned JSON), so these pin the parsing/citation/robustness logic — the
part that must never crash a check or present a hallucinated citation."""

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.components.compliance import (
    NEEDS_REVIEW,
    Requirement,
    Verdict,
    _parse_json_object,
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
