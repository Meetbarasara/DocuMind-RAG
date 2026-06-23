"""Regression test for SEC-8 (see BUGFIXES.md).

ingestion.py (and utils._log_elements_analysis) used print() for routine
progress messages and the one real error case, bypassing the configured
logger entirely — no log level filtering, no rotation, output goes
straight to stdout regardless of how the app's logging is configured in
production.

The success-path prints inside build_langchain_documents need real
unstructured element internals (chunk_by_title relies on them) to
exercise end-to-end without network-dependent partition calls, so those
are verified via a static grep instead — see test_no_print_in_ingestion_methods.
"""

import logging
from pathlib import Path

from src.components.config import Config
from src.components.ingestion import DocumentProcessor
from src.utils import _log_elements_analysis


def test_log_elements_analysis_logs_instead_of_printing(capsys, caplog):
    class FakeElement:
        category = "Text"

    with caplog.at_level(logging.DEBUG, logger="src.utils"):
        _log_elements_analysis([FakeElement(), FakeElement()])

    captured = capsys.readouterr()
    assert captured.out == "", f"_log_elements_analysis wrote to stdout: {captured.out!r}"
    assert any("Text" in r.message for r in caplog.records), "expected the breakdown in the log records"


def test_process_documents_failure_logs_instead_of_printing(capsys, caplog):
    processor = DocumentProcessor(Config())

    with caplog.at_level(logging.ERROR, logger="src.components.ingestion"):
        result = processor.process_documents("nonexistent_file.unsupported_extension")

    assert result == []
    captured = capsys.readouterr()
    assert captured.out == "", f"process_documents wrote to stdout: {captured.out!r}"
    assert any("nonexistent_file" in r.message for r in caplog.records)


def test_no_print_in_production_ingestion_methods():
    """Static check for the prints that need real unstructured internals to
    exercise end-to-end (chunk_by_title doesn't accept duck-typed fakes)."""
    ingestion_path = Path(__file__).parent.parent / "src" / "components" / "ingestion.py"
    source = ingestion_path.read_text(encoding="utf-8")
    main_block_start = source.index('if __name__ == "__main__"')
    production_code = source[:main_block_start]

    assert "print(" not in production_code, (
        "found print() in ingestion.py outside the __main__ block — "
        "should use the logger instead"
    )
