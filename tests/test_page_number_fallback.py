"""Regression test for BUG-11 (see BUGFIXES.md).

_get_page_number only read an element's own metadata.page_number. For a
composite chunk (chunk_by_title's output) that ends up with no page_number
of its own, but still carries the pre-chunking original elements via
metadata.orig_elements, this returned None even when a real page number
was recoverable from one of those originals.

Confirmed empirically against the actually-installed unstructured==0.22.10
that chunk_by_title *usually* already sets the composite's own page_number
to the first original element's page (contradicting the original review's
claim that it's "frequently None" from merging across pages) — None only
shows up when every original element genuinely lacks a page_number (e.g.
non-paginated formats), which is correct, not a bug. This fallback is
defensive: it doesn't depend on that being true for every library version
or edge case.
"""

from types import SimpleNamespace

from src.utils import _get_page_number


def _fake_element(page_number=None, orig_elements=None):
    return SimpleNamespace(metadata=SimpleNamespace(page_number=page_number, orig_elements=orig_elements))


def test_returns_own_page_number_when_present():
    el = _fake_element(page_number=3)
    assert _get_page_number(el) == 3


def test_falls_back_to_first_orig_element_with_a_page_number():
    composite = _fake_element(
        page_number=None,
        orig_elements=[
            _fake_element(page_number=None),
            _fake_element(page_number=5),
            _fake_element(page_number=6),
        ],
    )
    assert _get_page_number(composite) == 5, "should recover page 5 from orig_elements, not give up at None"


def test_returns_none_when_nothing_has_a_page_number():
    composite = _fake_element(
        page_number=None,
        orig_elements=[_fake_element(page_number=None), _fake_element(page_number=None)],
    )
    assert _get_page_number(composite) is None
