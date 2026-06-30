"""run_eval.py — offline RAG evaluation harness (Pillar E).

Runs the pipeline over a versioned gold set and reports BOTH layers:
  - Retrieval:  Hit@k / Recall@k / MRR (page-level, from the labeled relevant_pages)
  - Generation: RAGAS faithfulness / answer_relevancy / context_precision / recall
  - Unanswerable: refusal rate on the negative rows

This needs real GOOGLE_API_KEY + PINECONE_API_KEY (it ingests the source doc and
makes LLM calls), so it's an offline script you run on demand — NOT a unit test.
It saves a baseline JSON so future runs can be compared (the basis for a CI
regression gate, Pillar E2).

Usage:
    python -m scripts.run_eval                 # uses data/eval/goldset.v1.jsonl
    python -m scripts.run_eval path/to/set.jsonl
"""

import asyncio
import json
import sys
from pathlib import Path

from src.components.config import Config
from src.components.evalution import (
    EvaluationManager,
    hit_at_k,
    load_goldset,
    mrr,
    recall_at_k,
    regression_failures,
)
from src.logger import get_logger
from src.pipeline.pipeline import RAGPipeline

logger = get_logger(__name__)

_ROOT = Path(__file__).parent.parent
_EVAL_NAMESPACE = "eval"
_REFUSAL_MARKERS = ("cannot find", "could not find", "couldn't find", "does not provide", "not provide")


def _is_refusal(answer: str) -> bool:
    a = (answer or "").lower()
    return any(m in a for m in _REFUSAL_MARKERS)


async def run(goldset_path: str) -> dict:
    cfg = Config()
    pipeline = RAGPipeline(cfg)
    gold = load_goldset(goldset_path)
    if not gold:
        raise SystemExit(f"Empty gold set: {goldset_path}")

    # Ingest each distinct source doc into the eval namespace (idempotent upsert).
    docs_dir = _ROOT / "docs"
    for src in {row["source_file"] for row in gold}:
        path = docs_dir / src
        if path.exists():
            pipeline.ingest_file(str(path), user_id=_EVAL_NAMESPACE, namespace=_EVAL_NAMESPACE)
        else:
            logger.warning("Gold source not found, skipping ingest: %s", path)

    rm = pipeline._get_retrieval_manager(_EVAL_NAMESPACE)

    retrieval_scores, gen_rows, refusals, per_row = [], [], [], []

    for row in gold:
        q = row["question"]
        relevant = row.get("relevant_pages", [])

        retrieved = rm.retrieve(q)
        pages = [d.metadata.get("page_number") for d in retrieved if d.metadata.get("page_number")]
        contexts = [d.page_content for d in retrieved]
        k = len(retrieved) or cfg.TOP_K

        result = await pipeline.generation_manager.generate(q, retrieved)
        answer = result["answer"]

        rowscore = {"id": row["id"], "category": row.get("category", "")}
        if relevant:                       # answerable -> retrieval metrics
            rs = {
                "hit@k": hit_at_k(pages, relevant, k),
                "recall@k": recall_at_k(pages, relevant, k),
                "mrr": mrr(pages, relevant),
            }
            retrieval_scores.append(rs)
            rowscore.update(rs)
        else:                              # unanswerable -> did it refuse?
            refused = _is_refusal(answer)
            refusals.append(refused)
            rowscore["refused"] = refused

        gen_rows.append({
            "question": q, "answer": answer,
            "contexts": contexts, "ground_truth": row.get("ground_truth", ""),
        })
        per_row.append(rowscore)
        print(f"  [{row['id']}] {row.get('category', '')}: {rowscore}")

    retrieval_summary = {}
    if retrieval_scores:
        for key in ("hit@k", "recall@k", "mrr"):
            retrieval_summary[key] = sum(s[key] for s in retrieval_scores) / len(retrieval_scores)

    ragas = EvaluationManager(cfg).evaluate_batch(gen_rows)
    gen_summary = ragas.get("summary", {}) if isinstance(ragas, dict) else {}

    summary = {
        "goldset": str(goldset_path),
        "n": len(gold),
        "retrieval": retrieval_summary,
        "generation": gen_summary,
        "refusal_rate": (sum(refusals) / len(refusals)) if refusals else None,
    }
    return {"summary": summary, "rows": per_row}


def _print_summary(out: dict) -> None:
    s = out["summary"]
    print("\n" + "=" * 60)
    print("EVAL SUMMARY")
    print("=" * 60)
    print(f"gold set: {s['goldset']}  (n={s['n']})")
    print("\nRetrieval (page-level):")
    for k, v in (s["retrieval"] or {}).items():
        print(f"  {k:<12} {v:.3f}")
    print("\nGeneration (RAGAS):")
    for k, v in (s["generation"] or {}).items():
        print(f"  {k:<24} {v:.3f}")
    if s["refusal_rate"] is not None:
        print(f"\nUnanswerable refusal rate: {s['refusal_rate']:.3f}")


def _flat_metrics(summary: dict) -> dict:
    """Flatten the nested summary to one {metric: value} dict for comparison."""
    flat = {}
    flat.update(summary.get("retrieval") or {})
    flat.update(summary.get("generation") or {})
    if summary.get("refusal_rate") is not None:
        flat["refusal_rate"] = summary["refusal_rate"]
    return flat


def main():
    args = sys.argv[1:]
    check = "--check" in args
    positional = [a for a in args if not a.startswith("-")]
    goldset_path = positional[0] if positional else str(_ROOT / "data" / "eval" / "goldset.v1.jsonl")

    out = asyncio.run(run(goldset_path))
    _print_summary(out)

    latest = _ROOT / "data" / "eval" / "baseline.json"
    latest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved latest run -> {latest}")

    if not check:
        return

    # E2 regression gate: compare this run to the committed reference baseline.
    ref_path = _ROOT / "data" / "eval" / "baseline.committed.json"
    if not ref_path.exists():
        print(f"\n[--check] no committed baseline at {ref_path} — skipping the gate. "
              "Commit a good run's baseline.json as baseline.committed.json to enable it.")
        return
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    failures = regression_failures(_flat_metrics(out["summary"]), _flat_metrics(ref.get("summary", {})))
    if failures:
        print("\nEVAL REGRESSION vs committed baseline:")
        for metric, base, cur in failures:
            print(f"  {metric}: baseline {base:.3f} -> current {cur:.3f}")
        sys.exit(1)
    print("\nNo eval regression vs committed baseline.")


if __name__ == "__main__":
    main()
