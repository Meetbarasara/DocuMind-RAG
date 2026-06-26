"""B-hybrid sub-step 2: page-snapshot storage.

The deterministic storage key is per-namespace and traversal-safe, and
pipeline.ingest_file uploads a snapshot for each visual page (best-effort,
skipped entirely when no db / image-answering is off).
"""

from langchain_core.documents import Document

from src.components.config import Config
from src.components.database import SupabaseManager
from src.pipeline.pipeline import RAGPipeline


def test_page_image_path_is_namespaced_and_sanitized():
    assert SupabaseManager._page_image_path("ns1", "report.pdf", 3) == "pages/ns1/report.pdf/3.png"
    # SEC-2: a traversal attempt in the filename is reduced to a basename
    assert SupabaseManager._page_image_path("ns1", "../../evil.pdf", 1) == "pages/ns1/evil.pdf/1.png"


class _FakeDB:
    def __init__(self):
        self.uploads = []

    def upload_page_image(self, namespace, filename, page_number, data):
        self.uploads.append((namespace, filename, page_number, bytes(data)))


def test_ingest_stores_a_snapshot_for_each_visual_page(monkeypatch, tmp_path):
    p = RAGPipeline(Config(), db=_FakeDB())

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    parsed = {
        "filename": "doc.pdf", "filepath": str(f), "filetype": "pdf",
        "pages": [(1, "text")], "images": [],
        "visual_page_images": {3: b"PNG-PAGE-3", 4: b"PNG-PAGE-4"},
    }
    monkeypatch.setattr(p.processor, "process_documents", lambda fp: parsed)
    monkeypatch.setattr(
        p.processor, "build_langchain_documents",
        lambda parsed: [Document(page_content="c", metadata={"filename": "doc.pdf"})],
    )
    monkeypatch.setattr(p.embedding_manager, "create_vector_store", lambda docs, namespace=None: None)
    monkeypatch.setattr(p.config, "USE_HYBRID_SEARCH", False)  # skip BM25 path

    p.ingest_file(str(f), user_id="ns1", namespace="ns1")

    assert {u[2] for u in p.db.uploads} == {3, 4}          # both visual pages stored
    assert all(u[0] == "ns1" and u[1] == "doc.pdf" for u in p.db.uploads)


def test_no_db_means_no_storage_attempt(monkeypatch, tmp_path):
    p = RAGPipeline(Config(), db=None)  # e.g. unit context

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    parsed = {
        "filename": "doc.pdf", "filepath": str(f), "filetype": "pdf",
        "pages": [(1, "t")], "images": [], "visual_page_images": {3: b"x"},
    }
    monkeypatch.setattr(p.processor, "process_documents", lambda fp: parsed)
    monkeypatch.setattr(
        p.processor, "build_langchain_documents",
        lambda parsed: [Document(page_content="c", metadata={"filename": "doc.pdf"})],
    )
    monkeypatch.setattr(p.embedding_manager, "create_vector_store", lambda docs, namespace=None: None)
    monkeypatch.setattr(p.config, "USE_HYBRID_SEARCH", False)

    # Should simply not raise (and there's no db to call).
    assert p.ingest_file(str(f), user_id="ns1", namespace="ns1") == 1
