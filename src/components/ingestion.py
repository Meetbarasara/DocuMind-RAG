from pathlib import Path
from typing import List

from langchain_core.documents import Document
from unstructured.chunking.title import chunk_by_title
from unstructured.partition.csv import partition_csv
from unstructured.partition.docx import partition_docx
from unstructured.partition.email import partition_email
from unstructured.partition.html import partition_html
from unstructured.partition.json import partition_json
from unstructured.partition.pdf import partition_pdf
from unstructured.partition.pptx import partition_pptx
from unstructured.partition.text import partition_text
from unstructured.partition.xlsx import partition_xlsx
from unstructured.partition.xml import partition_xml

from src.utils import (
    _create_image_description,
    _create_table_description,
    _element_has_image_payload,
    _get_element_type,
    _get_metadata_fields,
    _get_page_number,
    _log_elements_analysis,
    _table_html,
)

try:
    from .config import Config
except ImportError:
    from src.components.config import Config

from src.logger import get_logger

logger = get_logger(__name__)


# ── File-extension → partition function mapping ───────────────────────────
# Each value is either a callable(filename) or a dict of kwargs for richer calls.
# This replaces the long if/elif chain while keeping the exact same behaviour.

_PARTITION_MAP = {
    # PDF strategy is injected at runtime by DocumentProcessor (see _get_pdf_partitioner)
    ".docx": lambda f: partition_docx(filename=f, infer_table_structure=True),
    ".pptx": lambda f: partition_pptx(filename=f),
    ".xlsx": lambda f: partition_xlsx(filename=f),
    ".txt":  lambda f: partition_text(filename=f),
    ".md":   lambda f: partition_text(filename=f),
    ".csv":  lambda f: partition_csv(filename=f),
    ".html": lambda f: partition_html(filename=f),
    ".htm":  lambda f: partition_html(filename=f),
    ".json": lambda f: partition_json(filename=f),
    ".xml":  lambda f: partition_xml(filename=f),
    ".eml":  lambda f: partition_email(filename=f),
    ".msg":  lambda f: partition_email(filename=f),
}


# ── Metadata fields extracted per chunk type ──────────────────────────────

_BASE_META_FIELDS = ["filepath", "filename", "filetype"]
_IMAGE_EXTRA_FIELDS = ["image_base64", "image_path"]


class DocumentProcessor:
    def __init__(self, config):
        self.config = config

    # ── PDF partition function (strategy depends on config) ──────────

    def _get_pdf_partitioner(self):
        """Return the appropriate PDF partitioner based on config strategy.

        Strategies:
            - ``"fast"``   — pdfminer text extraction. ~2-5 seconds per PDF.
                             Best for text-heavy documents. **Default.**
            - ``"hi_res"`` — ML-based layout detection (detectron2/YOLOX).
                             ~120-200s on CPU. Use for table/image-heavy PDFs
                             or when you have a GPU.
            - ``"auto"``   — Uses ``fast`` unless the PDF contains images,
                             in which case it falls back to ``hi_res``.
        """
        strategy = getattr(self.config, "PDF_PARSE_STRATEGY", "fast")

        if strategy == "hi_res":
            return lambda f: partition_pdf(
                filename=f,
                strategy="hi_res",
                infer_table_structure=True,
                extract_image_block_types=["Image"],
                extract_image_block_to_payload=True,
            )
        else:
            # "fast" or "auto" — use pdfminer, skip ML inference
            return lambda f: partition_pdf(
                filename=f,
                strategy="fast",
                extract_images_in_pdf=False,
            )

    # ── Step 1: Parse raw file into unstructured elements ─────────────

    def process_documents(self, file_paths: str) -> List:
        """Parse a single file and return a list of unstructured elements."""
        path = Path(file_paths)
        extension = path.suffix.lower()
        file_name = path.name

        try:
            if extension == ".pdf":
                partition_fn = self._get_pdf_partitioner()
            else:
                partition_fn = _PARTITION_MAP.get(extension)

            if partition_fn is None:
                raise ValueError(f"Unsupported file type: {extension}")

            elements = partition_fn(file_paths)
            logger.debug("Processed %s successfully. Extracted %d elements.", file_paths, len(elements))

            _log_elements_analysis(elements)

            # Stamp source metadata onto every element
            for element in elements:
                if hasattr(element, "metadata"):
                    element.metadata.filename = file_name
                    element.metadata.filetype = extension.strip(".")
                    element.metadata.filepath = file_paths

            return elements

        except Exception as e:
            logger.error("Error processing %s: %s", file_paths, e, exc_info=True)
            return []

    # ── Step 2: Convert elements → LangChain Documents ───────────────

    def build_langchain_documents(self, elements: List) -> List[Document]:
        if not elements:
            return []

        # Classify each element into text / table / image buckets
        text_elements, table_elements, image_elements = [], [], []

        for el in elements:
            el_type = _get_element_type(el).lower()

            if "table" in el_type:
                table_elements.append(el)
            elif "image" in el_type or _element_has_image_payload(el):
                image_elements.append(el)
            elif getattr(el, "text", None):
                text_elements.append(el)

        docs: List[Document] = []

        # ── Text chunks ──────────────────────────────────────────────
        if text_elements:
            logger.debug("Chunking TEXT elements by title...")
            text_chunks = chunk_by_title(
                elements=text_elements,
                max_characters=self.config.CHUNK_SIZE,
                new_after_n_chars=self.config.NEW_AFTER_N_CHARS,
                combine_text_under_n_chars=self.config.COMBINE_TEXT_UNDER_N_CHARS,
                # Logical Mistake #8 fix: CHUNK_OVERLAP was configured but
                # never passed, so no overlap was ever applied. overlap_all
                # is required too -- without it, `overlap` only applies when
                # a single oversized element gets mid-text split, not
                # between normal chunks formed from separate elements.
                overlap=self.config.CHUNK_OVERLAP,
                overlap_all=True,
            )

            for i, chunk in enumerate(text_chunks, start=1):
                chunk_text = (chunk.text or "").strip()
                if len(chunk_text) < 10:
                    continue

                meta = _get_metadata_fields(chunk, _BASE_META_FIELDS)
                meta.update({
                    "chunk_type": "text",
                    "source": meta["filepath"] or meta["filename"],
                    # BUG-11 fix: text chunks pulled page_number straight
                    # off the chunk's own metadata, with no fallback —
                    # table/image chunks already use _get_page_number,
                    # which now also falls back to orig_elements (the
                    # pre-chunking elements) if the composite's own
                    # page_number is missing.
                    "page_number": _get_page_number(chunk),
                    "chunk_index": i,
                    # BUG-12 fix: chunk_id was set here via _stable_id(...),
                    # then unconditionally overwritten by embeddings.py's
                    # own scheme right before upsert — this assignment was
                    # dead. Removed (here and in the table/image branches
                    # below) rather than computing an id that's never used.
                })
                docs.append(Document(page_content=chunk_text, metadata=meta))

            logger.debug("Created %d TEXT chunks.", len(text_chunks))

        # ── Table chunks ─────────────────────────────────────────────
        if table_elements:
            logger.debug("Creating TABLE chunks...")

            for i, el in enumerate(table_elements, start=1):
                html = _table_html(el)
                table_text = (html if html else (el.text or "")).strip()
                if not table_text:
                    continue

                meta = _get_metadata_fields(el, _BASE_META_FIELDS)
                meta.update({
                    "chunk_type": "table",
                    "source": meta["filepath"],
                    "page_number": _get_page_number(el),
                    "chunk_index": i,
                    "table_format": "html" if html else "text",
                    "description": _create_table_description(el),
                })
                docs.append(Document(page_content=table_text, metadata=meta))

            logger.debug("Created %d TABLE chunks.", len(table_elements))

        # ── Image chunks ─────────────────────────────────────────────
        if image_elements:
            logger.debug("Creating IMAGE chunks...")

            for i, el in enumerate(image_elements, start=1):
                page_number = _get_page_number(el)
                image_text = _create_image_description(el, page_number)

                meta = _get_metadata_fields(el, _BASE_META_FIELDS + _IMAGE_EXTRA_FIELDS)
                meta.update({
                    "chunk_type": "image",
                    "source": meta["filepath"],
                    "page_number": page_number,
                    "chunk_index": i,
                    "has_image_payload": bool(meta.pop("image_base64", None)),
                    "image_path": meta.pop("image_path", None),
                    "description": image_text,
                })
                docs.append(Document(page_content=image_text, metadata=meta))

            logger.debug("Created %d IMAGE chunks.", len(image_elements))

        logger.debug("Total LangChain Documents created: %d", len(docs))
        return docs


if __name__ == "__main__":
    config = Config()
    processor = DocumentProcessor(config)

    # Absolute path: project_root / docs / <pdf>
    pdf_path = str(Path(__file__).parent.parent.parent / "docs" / "Smart_Signal__Adaptive_Traffic_Signal_Control_using_Reinforcement_Learning_and_Object_Detection.pdf")
    print(f"\n{'='*60}")
    print(f"Ingestion test — {pdf_path}")
    print(f"{'='*60}")

    elements = processor.process_documents(pdf_path)
    langchain_docs = processor.build_langchain_documents(elements)

    print(f"\nFinal LangChain Documents: {len(langchain_docs)}")
    for i, doc in enumerate(langchain_docs[:3], 1):
        print(f"\n  [{i}] type={doc.metadata.get('chunk_type')} | page={doc.metadata.get('page_number')}")
        print(f"      {doc.page_content[:120]}...")
    print("\n✅ Ingestion test complete!")