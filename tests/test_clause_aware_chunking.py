"""Clause/section-aware chunking for legal text (regulations).

Pins the structure-aware splitter that keeps whole clauses together instead of
cutting fixed-size token windows mid-clause. Pure/keyless — the token counter and
oversized-clause splitter are passed in, so packing logic is deterministic.
"""

from types import SimpleNamespace

import pytest

from src.components.ingestion import (
    DocumentProcessor,
    _CLAUSE_MARKER,
    _clause_units,
    split_legal_text,
)


# ── Marker detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "1. text", "1.2 text", "1.2.3. text", "10) text",
    "(a) text", "(iv) text", "(12) text", "a. text", "i. text",
    "Section 5 duties", "CHAPTER II definitions", "Article 3 scope", "Rule 9 filing",
])
def test_marker_matches_clause_starts(line):
    assert _CLAUSE_MARKER.match(line)


@pytest.mark.parametrize("line", [
    "The bank shall verify identity.",
    "shall retain records for five years",
    "5 years is the minimum period",     # bare number, no clause punctuation
    "e.g. an illustrative example",      # abbreviation, no space after "e."
    "U.S. persons are excluded",         # "U." has no trailing space
    "",
])
def test_non_marker_lines_do_not_match(line):
    assert not _CLAUSE_MARKER.match(line)


def test_clause_units_split_on_markers_with_preamble():
    text = (
        "Preamble sentence.\n"
        "1. First clause body.\n"
        "2. Second clause body.\n"
        "(a) a sub-point of two\n"
        "Section 3 a heading\n"
        "trailing body line"
    )
    units = _clause_units(text)
    assert len(units) == 5
    assert units[0].startswith("Preamble")
    assert units[1].startswith("1.")
    assert units[3].startswith("(a)")
    assert units[4] == "Section 3 a heading\ntrailing body line"   # body attaches to its marker


# ── Packing (deterministic: word-count as tokens) ───────────────────────────

def _wc(s: str) -> int:
    return len(s.split())


def _fake_token_split(s: str):
    """Stand-in recursive splitter: cut an oversized clause into <=5-word pieces."""
    words = s.split()
    return [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]


def test_packs_whole_clauses_up_to_budget():
    text = "1. Alpha beta.\n2. Gamma delta.\n3. Epsilon zeta."
    out = split_legal_text(text, _fake_token_split, _wc, 6)   # ~3 words/clause -> 2 per chunk
    assert len(out) == 2
    assert "Alpha" in out[0] and "Gamma" in out[0]            # two clauses packed together
    assert "Epsilon" in out[1]


def test_oversized_clause_falls_back_to_token_split():
    big = "1. " + " ".join(f"w{i}" for i in range(20))        # 21 words, over the budget
    out = split_legal_text(big, _fake_token_split, _wc, 6)
    assert len(out) >= 4
    assert all(len(c.split()) <= 5 for c in out)              # split into the fallback's pieces


def test_no_markers_degrades_to_a_single_unit():
    text = "This is plain prose with no clause markers here at all."
    assert split_legal_text(text, _fake_token_split, _wc, 100) == [text]


def test_empty_text_yields_no_chunks():
    assert split_legal_text("", _fake_token_split, _wc, 10) == []


# ── Integration: build_langchain_documents(clause_aware=True) ────────────────

def test_build_documents_clause_aware_keeps_each_clause_whole():
    cfg = SimpleNamespace(CHUNK_SIZE_TOKENS=40, CHUNK_OVERLAP_TOKENS=8)
    proc = DocumentProcessor(cfg)
    clauses = [
        "The reporting entity shall verify identity using an Officially Valid Document.",
        "Records shall be retained for five years after the account is closed.",
        "High-risk customers shall undergo enhanced due diligence and source-of-funds checks.",
    ]
    text = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(clauses))
    parsed = {
        "filename": "reg.pdf", "filepath": "/x/reg.pdf", "filetype": "pdf",
        "pages": [(1, text)], "visual_page_images": {},
    }
    docs = proc.build_langchain_documents(parsed, clause_aware=True)
    assert docs
    # every clause body sits INTACT inside a single chunk — never cut across chunks
    for clause in clauses:
        assert any(clause in d.page_content for d in docs), clause
    assert all(d.metadata["page_number"] == 1 for d in docs)   # page attribution preserved
