"""Regression guard for SEC-8 (see BUGFIXES.md).

Ingestion must use the logger, never print(), so output respects log
levels/rotation and never leaks document content straight to stdout.

Updated for the PyMuPDF/python-docx rewrite (Q1/B2): the old unstructured
element helpers (and their _log_elements_analysis print path) are gone, so this
now guards the new parse path instead.
"""

import logging
from pathlib import Path

from src.components.config import Config
from src.components.ingestion import DocumentProcessor


def test_process_documents_failure_logs_instead_of_printing(capsys, caplog):
    processor = DocumentProcessor(Config())

    with caplog.at_level(logging.ERROR, logger="src.components.ingestion"):
        result = processor.process_documents("nonexistent_file.weirdext")

    assert result == {}  # failure returns an empty parse, not a crash
    captured = capsys.readouterr()
    assert captured.out == "", f"process_documents wrote to stdout: {captured.out!r}"
    assert any("nonexistent_file" in r.message for r in caplog.records)


def test_no_print_in_production_ingestion_methods():
    """Static check: no print() in ingestion.py outside the __main__ block."""
    ingestion_path = Path(__file__).parent.parent / "src" / "components" / "ingestion.py"
    source = ingestion_path.read_text(encoding="utf-8")
    main_block_start = source.index('if __name__ == "__main__"')
    production_code = source[:main_block_start]

    assert "print(" not in production_code, (
        "found print() in ingestion.py outside the __main__ block — "
        "should use the logger instead"
    )
