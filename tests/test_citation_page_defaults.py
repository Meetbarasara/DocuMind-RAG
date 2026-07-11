"""Regression test for BUG-8 (see BUGFIXES.md).

_build_context_and_sources builds the context label shown to the LLM with
meta.get('page_number', 'N/A') — correctly defaulting to "N/A" when the key
is absent. But it built the *sources* list (used later for citation
verification) with meta.get("page_number") — no default, so a missing page
becomes None, not "N/A". The LLM, having seen "Page: N/A" in the context,
naturally cites "[Source: file, Page: N/A]" — but _verify_citations'
lookup set then contains the *string* "none" (str(None).lower()), not
"n/a", so a citation that's actually correct gets marked unverified.
"""

from langchain_core.documents import Document

from src.components.generation import AnswerGeneration, _fmt_page


def test_fmt_page_normalizes_float_and_missing():
    assert _fmt_page(2.0) == "2"        # Pinecone's hybrid path returns page as a float
    assert _fmt_page(2) == "2"
    assert _fmt_page(None) == "N/A"
    assert _fmt_page("N/A") == "N/A"


def test_build_context_normalizes_float_page_to_int():
    # Pinecone's hybrid retrieval rebuilds metadata with page_number as a float;
    # the citation must read "p.2", not "2.0", in both the label and the sources.
    doc = Document(
        page_content="text",
        metadata={"filename": "r.pdf", "page_number": 2.0, "chunk_type": "text"},
    )
    context, sources = AnswerGeneration._build_context_and_sources([doc])
    assert sources[0]["page"] == "2"
    assert "Page: 2," in context and "Page: 2.0" not in context


def test_build_context_and_sources_defaults_missing_page_to_na():
    doc = Document(
        page_content="some chunked text",
        metadata={"filename": "report.pdf", "chunk_type": "text"},  # no page_number key
    )

    _, sources = AnswerGeneration._build_context_and_sources([doc])

    assert sources[0]["page"] == "N/A", f"expected 'N/A', got {sources[0]['page']!r}"


def test_verify_citations_accepts_na_page_for_missing_page_number():
    sources = [{"filename": "report.pdf", "page": "N/A", "chunk_type": "text", "chunk_id": "x"}]
    answer = "The model uses YOLOv8. [Source: report.pdf, Page: N/A]"

    result = AnswerGeneration._verify_citations(answer, sources)

    assert result["unverified"] == [], f"a correct citation was marked unverified: {result}"
    assert "[Source: report.pdf, Page: N/A]" in result["verified"]
    assert result["score"] == 1.0
