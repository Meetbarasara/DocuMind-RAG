"""Compliance-eval extension: the pure macro-F1 metric, the labeled gold set's
well-formedness, and the judge harness wired to a fake LLM (keyless)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_compliance_eval import _flat_metrics, evaluate_goldset
from src.components.evalution import COMPLIANCE_STATUSES, compliance_metrics, load_goldset

_GOLDSET = Path(__file__).resolve().parents[1] / "data" / "eval" / "compliance_goldset.v1.jsonl"


class _FakeLLM:
    """Async chat stub — .content is a str or a fn of the messages (matches
    tests/test_compliance_engine.py)."""

    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages):
        c = self._content(messages) if callable(self._content) else self._content
        return SimpleNamespace(content=c)


# ── compliance_metrics (pure) ───────────────────────────────────────────────

def test_perfect_predictions_score_one():
    preds = ["Covered", "Partial", "Gap", "Conflict"]
    m = compliance_metrics(preds, list(preds))
    assert m["accuracy"] == 1.0 and m["macro_f1"] == 1.0


def test_all_wrong_scores_zero():
    labels = ["Covered", "Partial", "Gap", "Conflict"]
    preds = ["Gap", "Gap", "Partial", "Partial"]
    m = compliance_metrics(preds, labels)
    assert m["accuracy"] == 0.0 and m["macro_f1"] == 0.0


def test_needs_review_counts_as_a_miss_not_ignored():
    # an out-of-set prediction ("Needs review") must never match a valid label
    m = compliance_metrics(["Covered", "Needs review"], ["Covered", "Covered"])
    assert m["accuracy"] == 0.5
    # Covered: tp=1, fp=0, fn=1 -> precision 1.0, recall 0.5, f1 = 0.667
    assert round(m["per_status"]["Covered"]["f1"], 3) == 0.667
    assert m["per_status"]["Covered"]["support"] == 2


def test_macro_f1_only_averages_classes_present_in_labels():
    # Partial + Conflict never appear as labels -> they must not drag macro-F1 to 0
    m = compliance_metrics(["Covered", "Gap"], ["Covered", "Gap"])
    assert m["macro_f1"] == 1.0
    assert m["per_status"]["Partial"]["support"] == 0
    assert m["per_status"]["Conflict"]["support"] == 0


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        compliance_metrics(["Covered"], ["Covered", "Gap"])


def test_flat_metrics_exposes_per_status_f1_for_the_gate():
    m = compliance_metrics(["Covered", "Gap"], ["Covered", "Gap"])
    flat = _flat_metrics(m)
    assert flat["accuracy"] == 1.0 and flat["macro_f1"] == 1.0
    assert set(flat) >= {"f1_Covered", "f1_Partial", "f1_Gap", "f1_Conflict"}


# ── the shipped gold set ────────────────────────────────────────────────────

def test_goldset_is_well_formed_and_covers_all_four_statuses():
    gold = load_goldset(str(_GOLDSET))
    assert len(gold) >= 10
    seen = set()
    for row in gold:
        assert row.get("requirement", "").strip(), f"row {row.get('id')} missing requirement"
        assert row["expected_status"] in COMPLIANCE_STATUSES, row
        seen.add(row["expected_status"])
    assert seen == set(COMPLIANCE_STATUSES)  # every status is exercised


# ── the harness wiring (fake judge, keyless) ────────────────────────────────

def _keyword_judge(messages):
    """Decide a status from the requirement+excerpt text in the human message."""
    human = messages[-1].content.lower()
    if "beneficial owner" in human:
        return json.dumps({"status": "Gap", "policy_quote": "", "confidence": 0.9, "rationale": "absent"})
    if "five years" in human and "three years" in human:
        return json.dumps({"status": "Conflict", "policy_quote": "three years", "confidence": 0.95, "rationale": "3 vs 5"})
    # Covered: quote is a substring of the excerpt so the citation verifies
    return json.dumps({"status": "Covered",
                       "policy_quote": "every customer must submit an officially valid document",
                       "confidence": 0.9, "rationale": "met"})


def test_evaluate_goldset_maps_verdicts_to_predictions():
    gold = [
        {"id": "g", "requirement": "Identify the beneficial owner of legal entities.",
         "policy_excerpt": "Some unrelated onboarding text.", "expected_status": "Gap"},
        {"id": "x", "requirement": "Retain records for at least five years after closure.",
         "policy_excerpt": "Records are retained for a period of three years after the account is closed.",
         "expected_status": "Conflict"},
        {"id": "c", "requirement": "Identify every customer with an OVD.",
         "policy_excerpt": "At onboarding, every customer must submit an Officially Valid Document (OVD).",
         "expected_status": "Covered"},
    ]
    preds, labels, rows = asyncio.run(evaluate_goldset(gold, _FakeLLM(_keyword_judge), delay=0))
    assert labels == ["Gap", "Conflict", "Covered"]
    assert preds == ["Gap", "Conflict", "Covered"]          # exact wiring, incl. Gap's empty-quote path
    assert compliance_metrics(preds, labels)["accuracy"] == 1.0
    assert [r["id"] for r in rows] == ["g", "x", "c"]


def test_transient_needs_review_is_retried():
    """A free-tier 429 surfaces as 'Needs review'; the harness must retry it so a
    rate-limited row isn't recorded as a judgment miss."""
    class _FlakyLLM:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:                              # first call: unparseable -> Needs review
                return SimpleNamespace(content="high traffic, no json")
            return SimpleNamespace(content=json.dumps(
                {"status": "Gap", "policy_quote": "", "confidence": 0.9, "rationale": "x"}))

    flaky = _FlakyLLM()
    gold = [{"id": "g", "requirement": "identify beneficial owner",
             "policy_excerpt": "unrelated", "expected_status": "Gap"}]
    preds, _, _ = asyncio.run(evaluate_goldset(gold, flaky, delay=0, retries=2))
    assert preds == ["Gap"]          # recovered on retry, not left as "Needs review"
    assert flaky.calls == 2
