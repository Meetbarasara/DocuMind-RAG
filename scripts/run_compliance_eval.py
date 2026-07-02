"""run_compliance_eval.py — offline accuracy eval for the KYC gap-analysis judge.

Runs the judge over a labeled ``(requirement, policy_excerpt) -> status`` gold
set and reports accuracy + macro-F1 over the four statuses (Covered / Partial /
Gap / Conflict). This is *judge-only* (no retrieval): it isolates the quality of
the gap-analysis JUDGMENT, so a bad number points at the judge/prompt, not at
retrieval.

Needs a judge key (CEREBRAS_API_KEY by default — one LLM call per gold row), so
it's an on-demand script, not a unit test. Saves a baseline JSON and supports a
``--check`` regression gate, exactly like scripts/run_eval.py.

Usage:
    python -m scripts.run_compliance_eval
    python -m scripts.run_compliance_eval path/to/set.jsonl
    python -m scripts.run_compliance_eval --check   # gate vs committed baseline
"""

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.documents import Document

from src.components.compliance import NEEDS_REVIEW, Requirement, judge_requirement
from src.components.config import Config
from src.components.evalution import (
    compliance_metrics,
    load_goldset,
    regression_failures,
)
from src.components.judge import build_judge_llm
from src.logger import get_logger

logger = get_logger(__name__)

_ROOT = Path(__file__).parent.parent


async def evaluate_goldset(gold, judge_llm, *, delay: float = 1.0, retries: int = 3) -> tuple:
    """Judge each gold row → (predictions, labels, per_row).

    Sequential and *paced*: the free judge tier 429s ("queue_exceeded") even
    under sequential bursts, and judge_requirement swallows that as "Needs
    review" — which for an eval is a spurious miss, not a judgment error. So we
    space calls by ``delay`` and retry a "Needs review" up to ``retries`` times
    with backoff. Every gold row has a definite label, so retrying a Needs-review
    can only recover a rate-limited row and never masks a real result. Pass
    ``delay=0`` in tests to keep them fast.
    """
    preds, labels, rows = [], [], []
    for row in gold:
        req = Requirement(id=row.get("id", "req"), text=row["requirement"])
        excerpt = (row.get("policy_excerpt") or "").strip()
        docs = (
            [Document(page_content=excerpt, metadata={
                "filename": row.get("policy_filename", "policy.pdf"),
                "page_number": row.get("policy_page"),
            })]
            if excerpt else []
        )
        verdict = await judge_requirement(req, docs, judge_llm)
        attempt = 0
        while verdict.status == NEEDS_REVIEW and attempt < retries:
            attempt += 1
            if delay:
                await asyncio.sleep(delay * (attempt + 1))     # backoff past the queue limit
            verdict = await judge_requirement(req, docs, judge_llm)
        preds.append(verdict.status)
        labels.append(row["expected_status"])
        rows.append({
            "id": req.id, "expected": row["expected_status"],
            "predicted": verdict.status, "confidence": verdict.confidence,
        })
        if delay:
            await asyncio.sleep(delay)                          # pace to stay under the rate limit
    return preds, labels, rows


async def run(goldset_path: str) -> dict:
    cfg = Config()
    judge_llm = build_judge_llm(cfg)          # raises JudgeNotConfigured if no key
    gold = load_goldset(goldset_path)
    if not gold:
        raise SystemExit(f"Empty gold set: {goldset_path}")

    preds, labels, rows = await evaluate_goldset(gold, judge_llm)
    for r in rows:
        mark = "OK" if r["expected"] == r["predicted"] else "XX"
        print(f"  {mark} [{r['id']:>3}] expected={r['expected']:<9} predicted={r['predicted']}")

    metrics = compliance_metrics(preds, labels)
    return {
        "summary": {"goldset": str(goldset_path), "judge": f"{cfg.JUDGE_PROVIDER}/{cfg.JUDGE_MODEL}", **metrics},
        "rows": rows,
    }


def _flat_metrics(summary: dict) -> dict:
    """Flatten to {metric: float} for the regression gate (reuses run_eval's comparator)."""
    flat = {"accuracy": summary.get("accuracy", 0.0), "macro_f1": summary.get("macro_f1", 0.0)}
    for status, m in (summary.get("per_status") or {}).items():
        flat[f"f1_{status}"] = m["f1"]
    return flat


def _print_summary(out: dict) -> None:
    s = out["summary"]
    print("\n" + "=" * 60)
    print("COMPLIANCE JUDGE EVAL")
    print("=" * 60)
    print(f"gold set: {s['goldset']}  (n={s['n']})   judge: {s['judge']}")
    print(f"\naccuracy : {s['accuracy']:.3f}")
    print(f"macro-F1 : {s['macro_f1']:.3f}")
    print("\nper-status  (precision / recall / f1 / support):")
    for status, m in s["per_status"].items():
        print(f"  {status:<9} {m['precision']:.2f} / {m['recall']:.2f} / {m['f1']:.2f}  (n={m['support']})")


def main():
    args = sys.argv[1:]
    check = "--check" in args
    positional = [a for a in args if not a.startswith("-")]
    goldset_path = positional[0] if positional else str(
        _ROOT / "data" / "eval" / "compliance_goldset.v1.jsonl"
    )

    out = asyncio.run(run(goldset_path))
    _print_summary(out)

    latest = _ROOT / "data" / "eval" / "compliance_baseline.json"
    latest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved latest run -> {latest}")

    if not check:
        return

    ref_path = _ROOT / "data" / "eval" / "compliance_baseline.committed.json"
    if not ref_path.exists():
        print(f"\n[--check] no committed baseline at {ref_path} — skipping the gate. "
              "Commit a good run's compliance_baseline.json as compliance_baseline.committed.json to enable it.")
        return
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    failures = regression_failures(_flat_metrics(out["summary"]), _flat_metrics(ref.get("summary", {})))
    if failures:
        print("\nCOMPLIANCE EVAL REGRESSION vs committed baseline:")
        for metric, base, cur in failures:
            print(f"  {metric}: baseline {base:.3f} -> current {cur:.3f}")
        sys.exit(1)
    print("\nNo compliance-eval regression vs committed baseline.")


if __name__ == "__main__":
    main()
