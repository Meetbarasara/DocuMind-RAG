"""Regression test for the exception.py LOW nit (see BUGFIXES.md).

The review claimed CustomException's "enriched traceback" feature was
"effectively inert in the app" because most call sites pass only a string
message (no explicit `sys` argument). Verified empirically before
touching anything: every real call site in this codebase raises
CustomException from inside an `except:` block, where sys.exc_info() is
still populated even without explicitly passing `sys` — the default
fallback (`error_detail or sys`) already covers that case. The feature
actually works; the real, narrower bug is that `error_detail: sys` is a
nonsensical type hint (using the sys *module* as a type).
"""

from src.exception import CustomException


def test_enrichment_fires_when_raised_from_an_except_block():
    """The real-world pattern used by every actual call site in this app."""
    try:
        try:
            raise ValueError("boom")
        except Exception as e:
            raise CustomException(f"Wrapped: {e}") from e
    except CustomException as ce:
        assert "Wrapped: boom" in str(ce)
        assert "line number" in str(ce)
        assert __file__ in str(ce) or "test_custom_exception.py" in str(ce)


def test_falls_back_to_plain_message_with_no_active_exception():
    """The validation-check call sites (e.g. a missing env var) — no
    exception is being handled, so there's nothing to enrich with."""
    ce = CustomException("Something is not configured.")
    assert str(ce) == "Something is not configured."
