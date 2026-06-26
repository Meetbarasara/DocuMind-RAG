"""Pillar E: page-level retrieval metrics + gold-set loading (pure, no API).

The full eval run (scripts/run_eval.py) needs real keys, but its scoring
functions and the gold set are deterministic and tested here.
"""

from pathlib import Path

from src.components.evalution import hit_at_k, load_goldset, mrr, recall_at_k


def test_hit_at_k():
    assert hit_at_k([2, 5, 9], [5], 3) == 1.0
    assert hit_at_k([2, 5, 9], [5], 1) == 0.0   # page 5 isn't in the top-1
    assert hit_at_k([1, 2], [], 2) == 0.0       # nothing relevant -> 0


def test_recall_at_k():
    assert recall_at_k([4, 5, 1], [4, 5], 3) == 1.0
    assert recall_at_k([4, 1, 9], [4, 5], 3) == 0.5   # found 1 of 2 relevant pages
    assert recall_at_k([1, 2], [], 2) == 0.0


def test_mrr():
    assert mrr([9, 5, 4], [4]) == 1 / 3
    assert mrr([4, 9], [4]) == 1.0
    assert mrr([1, 2], [9]) == 0.0


def test_goldset_loads_and_is_well_formed():
    path = Path(__file__).parent.parent / "data" / "eval" / "goldset.v1.jsonl"
    rows = load_goldset(str(path))

    assert len(rows) >= 5
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "gold ids must be unique"
    for r in rows:
        assert r["question"] and r["ground_truth"] and "relevant_pages" in r
    # must include at least one negative/unanswerable row (tests refusal behaviour)
    assert any(r.get("category") == "unanswerable" for r in rows)
