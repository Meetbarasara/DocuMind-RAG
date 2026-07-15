"""FakeJudgeLLM (JUDGE_PROVIDER=fake) — the deterministic judge E2E tests run on.

It must speak the engine's real contract: valid verdict JSON whose quotes are
verbatim from the excerpts (so _verify_evidence attaches a citation), and a
valid extraction JSON for regulation chunks. If the engine's prompts or the
verifier change shape, these tests catch the drift before the E2E suite does.
"""

import pytest
from langchain_core.documents import Document

from src.components.compliance import (
    NEEDS_REVIEW,
    Requirement,
    extract_requirements,
    judge_requirement,
)
from src.components.config import Config
from src.components.judge import FakeJudgeLLM, JudgeNotConfigured, build_judge_llm


def _chunk(text, filename="acme_policy.pdf", page=2):
    return Document(page_content=text, metadata={"filename": filename, "page_number": page})


POLICY_TEXT = (
    "Customer records shall be retained for five years after the relationship ends. "
    "The Principal Officer reports suspicious transactions to FIU-IND."
)


def test_factory_returns_fake_without_any_key(monkeypatch):
    monkeypatch.setenv("JUDGE_PROVIDER", "fake")
    assert isinstance(build_judge_llm(Config()), FakeJudgeLLM)


def test_factory_still_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("JUDGE_PROVIDER", "nope")
    with pytest.raises(JudgeNotConfigured):
        build_judge_llm(Config())


def _req(mod3: int) -> Requirement:
    """A requirement whose text length % 3 == mod3 (drives the fake's status)."""
    text = "Records must be retained for at least five years after account closure"
    while len(text) % 3 != mod3:
        text += "!"
    return Requirement(id="req-x", text=text)


@pytest.mark.asyncio
@pytest.mark.parametrize("mod3,expected", [(0, "Covered"), (1, "Partial"), (2, "Gap")])
async def test_deterministic_status_and_verified_citation(mod3, expected):
    verdict = await judge_requirement(_req(mod3), [_chunk(POLICY_TEXT)], FakeJudgeLLM())
    assert verdict.status == expected  # never degrades to Needs review
    if expected in ("Covered", "Partial"):
        # Quote is verbatim → evidence verification must attach a real citation.
        assert verdict.policy_quote and verdict.policy_quote in POLICY_TEXT
        assert verdict.policy_filename == "acme_policy.pdf"
        assert verdict.policy_page == 2
        assert verdict.evidence_score >= 0.8
    else:
        assert verdict.policy_quote == ""
    assert verdict.status != NEEDS_REVIEW


@pytest.mark.asyncio
async def test_no_excerpts_is_always_a_gap():
    verdict = await judge_requirement(_req(0), [], FakeJudgeLLM())
    assert verdict.status == "Gap"
    assert verdict.policy_filename is None


@pytest.mark.asyncio
async def test_extraction_yields_requirements_with_pages():
    chunks = [_chunk(
        "Regulated entities shall verify customer identity at onboarding using an OVD. "
        "Records of identification data must be preserved for at least five years. "
        "Short line.", filename="rbi.pdf", page=7,
    )]
    reqs = await extract_requirements(chunks, FakeJudgeLLM())
    assert len(reqs) == 2  # the <40-char sentence is dropped
    assert all(r.page == 7 for r in reqs)
    assert "verify customer identity" in reqs[0].text
