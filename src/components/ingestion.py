"""ingestion.py — lightweight document parsing for DocuMind (Q1 + B2).

Replaces the heavy ``unstructured[all-docs]`` stack with direct, fast parsers:
    - PDF  → PyMuPDF (fitz): per-page text + embedded images
    - DOCX → python-docx: paragraphs + tables + embedded images
    - TXT  → plain read

Text is split on **token** boundaries (Q1) for predictable context size and
cost. Images are extracted here (bytes + page) but **not yet indexed** — the
image-answering step consumes ``parsed["images"]`` to store the real image and
add a vision-model description.
"""

import hashlib
from pathlib import Path
from typing import List, Tuple

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.logger import get_logger

logger = get_logger(__name__)

# Drop chunks too short to carry meaning, and images too small to be real
# content (icons, bullets, logos) — keeps the index clean and the future
# vision bill down.
_MIN_CHUNK_CHARS = 10
_MIN_IMAGE_BYTES = 3000


class DocumentProcessor:
    """Parse PDF / DOCX / TXT into token-sized LangChain Documents (+ images)."""

    def __init__(self, config):
        self.config = config
        # from_tiktoken_encoder makes the splitter count *tokens*, not chars.
        self._splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            chunk_size=config.CHUNK_SIZE_TOKENS,
            chunk_overlap=config.CHUNK_OVERLAP_TOKENS,
        )

    # ── Step 1: parse a file into a normalized dict ───────────────────────

    def process_documents(self, file_path: str) -> dict:
        """Parse *file_path* into a normalized dict, or ``{}`` on failure.

        Returns:
            {
              "filename", "filepath", "filetype",
              "pages":  [(page_number | None, text), ...],
              "images": [{"page_number", "ext", "bytes", "image_hash"}, ...],
            }
        """
        path = Path(file_path)
        ext = path.suffix.lower()
        try:
            if ext == ".pdf":
                pages, images = self._parse_pdf(file_path)
            elif ext == ".docx":
                pages, images = self._parse_docx(file_path)
            elif ext in (".txt", ".md"):
                pages, images = self._parse_txt(file_path)
            else:
                raise ValueError(f"Unsupported file type: {ext}")

            logger.debug(
                "Parsed %s: %d page(s), %d image(s) extracted (images deferred to "
                "the image-answering step)", path.name, len(pages), len(images),
            )
            return {
                "filename": path.name,
                "filepath": str(file_path),
                "filetype": ext.lstrip("."),
                "pages": pages,
                "images": images,
            }
        except Exception as e:
            logger.error("Error processing %s: %s", file_path, e, exc_info=True)
            return {}

    # ── Step 2: token-chunk the text into LangChain Documents ─────────────

    def build_langchain_documents(self, parsed: dict) -> List[Document]:
        if not isinstance(parsed, dict) or not parsed.get("pages"):
            return []

        docs: List[Document] = []
        for page_number, text in parsed["pages"]:
            text = (text or "").strip()
            if not text:
                continue
            for chunk in self._splitter.split_text(text):
                chunk = chunk.strip()
                if len(chunk) < _MIN_CHUNK_CHARS:
                    continue
                meta = {
                    "filename": parsed["filename"],
                    "filepath": parsed["filepath"],
                    "filetype": parsed["filetype"],
                    "chunk_type": "text",
                    "source": parsed["filepath"],
                }
                # Only set page_number when we actually have one (PDF). DOCX/TXT
                # have no pages, so leaving it off makes citations read "N/A"
                # instead of a misleading "page 1".
                if page_number is not None:
                    meta["page_number"] = page_number
                docs.append(Document(page_content=chunk, metadata=meta))

        logger.info("Built %d text chunk(s) from %s", len(docs), parsed.get("filename"))
        return docs

    # ── Parsers ───────────────────────────────────────────────────────────

    @staticmethod
    def _image_record(page_number, data: bytes, ext: str) -> dict:
        return {
            "page_number": page_number,
            "ext": ext or "png",
            "bytes": data,
            "image_hash": hashlib.sha256(data).hexdigest(),
        }

    def _parse_pdf(self, file_path) -> Tuple[list, list]:
        pages, images = [], []
        with fitz.open(file_path) as doc:
            for page_index, page in enumerate(doc, start=1):
                pages.append((page_index, page.get_text()))
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        base = doc.extract_image(xref)
                    except Exception:
                        continue
                    data = base.get("image", b"")
                    if len(data) >= _MIN_IMAGE_BYTES:
                        images.append(self._image_record(page_index, data, base.get("ext")))
        return pages, images

    def _parse_docx(self, file_path) -> Tuple[list, list]:
        doc = DocxDocument(file_path)

        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)

        images = []
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                try:
                    data = rel.target_part.blob
                except Exception:
                    continue
                if len(data) >= _MIN_IMAGE_BYTES:
                    images.append(self._image_record(None, data, "png"))

        # DOCX has no page concept → page_number None (citations show N/A).
        return [(None, text)], images

    def _parse_txt(self, file_path) -> Tuple[list, list]:
        text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        return [(None, text)], []


if __name__ == "__main__":
    from src.components.config import Config

    proc = DocumentProcessor(Config())
    sample = str(
        Path(__file__).parent.parent.parent
        / "docs"
        / "Smart_Signal__Adaptive_Traffic_Signal_Control_using_Reinforcement_Learning_and_Object_Detection.pdf"
    )
    parsed = proc.process_documents(sample)
    docs = proc.build_langchain_documents(parsed)
    print(
        f"pages={len(parsed.get('pages', []))} "
        f"images={len(parsed.get('images', []))} chunks={len(docs)}"
    )
    if docs:
        print("first chunk page:", docs[0].metadata.get("page_number"))
        print(docs[0].page_content[:200])
