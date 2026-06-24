"""Regression test for Logical Mistake #8 (see BUGFIXES.md / CODE_REVIEW.md §3).

config.CHUNK_OVERLAP=500 was defined but never passed to chunk_by_title, so
no overlap was ever actually applied between chunks -- contradicting the
README's "overlap 500" / "Overlap between chunks" claims.

The review's literal phrasing -- "chunk_by_title doesn't take an overlap
arg in this code" -- does NOT hold against the actually-installed
unstructured==0.22.10: chunk_by_title's signature includes both `overlap`
and `overlap_all`. Confirmed via the library's own source
(unstructured/chunking/base.py): `overlap` defaults to 0 when not passed,
and `overlap` only applies between chunks formed from whole elements (the
common case, not the "one giant element gets text-split" edge case) when
`overlap_all=True` is *also* passed -- neither was ever passed here. So
the review's diagnosis (CHUNK_OVERLAP is dead config) is correct even
though its stated mechanism is wrong; the fix needs both kwargs, not just
`overlap=`.
"""

import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import src.components.ingestion as ingestion_module
from src.components.config import Config
from src.components.ingestion import DocumentProcessor


def test_build_langchain_documents_passes_configured_overlap_to_chunk_by_title():
    config = Config()
    processor = DocumentProcessor(config)

    # Duck-typed fake -- chunk_by_title's return is mocked to [] below, so
    # the only thing that matters about this input element is that
    # build_langchain_documents classifies it as a *text* element (not
    # table/image) and therefore calls chunk_by_title on it at all.
    text_el = SimpleNamespace(category="UncategorizedText", text="some text", metadata=None)

    with patch.object(ingestion_module, "chunk_by_title", return_value=[]) as mock_chunk:
        processor.build_langchain_documents([text_el])

    assert mock_chunk.called, "chunk_by_title should be invoked for text elements"
    _, kwargs = mock_chunk.call_args
    assert kwargs.get("overlap") == config.CHUNK_OVERLAP, (
        f"expected overlap={config.CHUNK_OVERLAP}, got {kwargs.get('overlap')!r}"
    )
    assert kwargs.get("overlap_all") is True, (
        "overlap_all must also be passed -- without it, `overlap` only applies "
        "when a single oversized element gets mid-text split, not between "
        "normal chunks formed from separate elements (the common case)"
    )


@contextmanager
def _real_unstructured_import():
    """conftest.py replaces the *entire* `unstructured` package tree (not
    just chunking.title) with empty placeholder modules for other tests, to
    dodge partition_* modules hanging on NLTK/network on a sandboxed box.
    chunk_by_title and the plain Element classes need neither (confirmed:
    importing them directly takes well under a second, no network).

    Snapshot and remove every poisoned `unstructured*` sys.modules entry so
    a fresh, real import can happen, then restore the exact prior state
    afterward so other tests in the session are unaffected.
    """
    saved = {
        name: mod for name, mod in sys.modules.items()
        if name == "unstructured" or name.startswith("unstructured.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [
            n for n in sys.modules
            if n == "unstructured" or n.startswith("unstructured.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)


def test_chunk_by_title_overlap_semantics_against_real_library():
    """Prove against the real, installed library (not a fake) that omitting
    overlap/overlap_all produces zero shared text between consecutive
    chunks, while passing both produces real shared text -- the exact
    mechanism this bug and its fix depend on.
    """
    with _real_unstructured_import():
        import importlib

        real_chunk_by_title = importlib.import_module("unstructured.chunking.title").chunk_by_title
        elements_module = importlib.import_module("unstructured.documents.elements")
        Text, Title = elements_module.Text, elements_module.Title

        els = [
            Title(text="Section A"),
            Text(text="AAAA " * 100),
            Title(text="Section B"),
            Text(text="BBBB " * 100),
        ]

        no_overlap = real_chunk_by_title(
            elements=els, max_characters=600, new_after_n_chars=500, combine_text_under_n_chars=0,
        )
        with_overlap = real_chunk_by_title(
            elements=els, max_characters=600, new_after_n_chars=500, combine_text_under_n_chars=0,
            overlap=200, overlap_all=True,
        )

    def shares_text_across_any_boundary(chunks):
        return any(
            chunks[i].text[-50:].strip() in chunks[i + 1].text
            for i in range(len(chunks) - 1)
        )

    assert not shares_text_across_any_boundary(no_overlap), (
        "current ingestion.py call (no overlap args) should produce zero "
        "shared text between consecutive chunks"
    )
    assert shares_text_across_any_boundary(with_overlap), (
        "overlap=200, overlap_all=True should produce real shared text "
        "between consecutive chunks"
    )
