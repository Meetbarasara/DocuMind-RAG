"""Q3: citation verification is filename-level — page numbers are too noisy to
gate on (chunking, multi-page answers, and the LLM's own page-image reads in the
B-hybrid multimodal path can all legitimately disagree with a chunk's single
recorded page_number). A citation is "verified" iff its filename matches a real
retrieved source; the page number is kept in the citation string for human
display only and no longer affects verified/unverified.

This is distinct from the already-fixed BUG-8 (None vs "N/A" string mismatch,
see test_citation_page_defaults.py) — here the filename is right and the page is
a genuinely *different*, plausible number, which is the kind of noise Q3 targets.
"""

from src.components.generation import AnswerGeneration

_SOURCES = [{"filename": "report.pdf", "page": "3", "chunk_type": "text", "chunk_id": "a"}]


def test_correct_filename_with_mismatched_page_is_verified():
    answer = "The model uses YOLOv8. [Source: report.pdf, Page: 5]"

    result = AnswerGeneration._verify_citations(answer, _SOURCES)

    assert result["unverified"] == [], f"a correct-filename citation was marked unverified: {result}"
    assert "[Source: report.pdf, Page: 5]" in result["verified"]
    assert result["score"] == 1.0


def test_wrong_filename_is_still_unverified():
    """Filename-level, not no-op: a citation to a file that wasn't retrieved must still fail."""
    answer = "[Source: made_up_file.pdf, Page: 1]"

    result = AnswerGeneration._verify_citations(answer, _SOURCES)

    assert result["verified"] == []
    assert "[Source: made_up_file.pdf, Page: 1]" in result["unverified"]
    assert result["score"] == 0.0


def test_matches_by_filename_among_multiple_sources_regardless_of_page():
    sources = [
        {"filename": "a.pdf", "page": "1", "chunk_type": "text", "chunk_id": "1"},
        {"filename": "b.pdf", "page": "9", "chunk_type": "text", "chunk_id": "2"},
    ]
    answer = "[Source: b.pdf, Page: 1]"   # right file, page belongs to neither chunk

    result = AnswerGeneration._verify_citations(answer, sources)

    assert result["verified"] == ["[Source: b.pdf, Page: 1]"]
    assert result["score"] == 1.0
