"""Q1+B2: token-based ingestion via PyMuPDF.

Runs against the committed sample PDF to prove the new lightweight path:
chunks are token-bounded and carry the right metadata (real PDF page numbers),
and embedded images are *extracted* but not yet indexed (text-first).
"""

from pathlib import Path

from src.components.config import Config
from src.components.ingestion import DocumentProcessor

_SAMPLE_PDF = (
    Path(__file__).parent.parent
    / "docs"
    / "Smart_Signal__Adaptive_Traffic_Signal_Control_using_Reinforcement_Learning_and_Object_Detection.pdf"
)


def test_pdf_parses_into_token_chunks_with_metadata():
    proc = DocumentProcessor(Config())
    parsed = proc.process_documents(str(_SAMPLE_PDF))
    docs = proc.build_langchain_documents(parsed)

    assert parsed["filetype"] == "pdf"
    assert len(parsed["pages"]) >= 1
    assert docs, "expected at least one text chunk"

    d = docs[0]
    assert d.metadata["chunk_type"] == "text"
    assert d.metadata["filename"].endswith(".pdf")
    assert isinstance(d.metadata["page_number"], int)  # real PDF page number
    assert d.page_content.strip()


def test_images_are_extracted_but_not_indexed_yet():
    proc = DocumentProcessor(Config())
    parsed = proc.process_documents(str(_SAMPLE_PDF))
    docs = proc.build_langchain_documents(parsed)

    # the sample PDF embeds images -> extracted into parsed["images"] ...
    assert len(parsed["images"]) >= 1
    for img in parsed["images"]:
        assert img["bytes"] and img["image_hash"]
    # ... but text-first: only text chunks are indexed for now
    assert all(d.metadata["chunk_type"] == "text" for d in docs)


def test_chunks_respect_the_token_budget():
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    cfg = Config()
    proc = DocumentProcessor(cfg)
    parsed = proc.process_documents(str(_SAMPLE_PDF))
    docs = proc.build_langchain_documents(parsed)

    # No chunk should blow past the configured token budget (small margin for
    # the splitter's boundary handling).
    for d in docs:
        assert len(enc.encode(d.page_content)) <= cfg.CHUNK_SIZE_TOKENS + 50
