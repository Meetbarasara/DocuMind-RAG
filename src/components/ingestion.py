import sys
from typing import List
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from unstructured.partition.csv import partition_csv
from unstructured.partition.docx import partition_docx
from unstructured.partition.email import partition_email
from unstructured.partition.html import partition_html
from unstructured.partition.json import partition_json
from unstructured.partition.pdf import partition_pdf
from unstructured.partition.pptx import partition_pptx
from unstructured.partition.text import partition_text
from unstructured.partition.xml import partition_xml
from unstructured.partition.xlsx import partition_xlsx
from unstructured.chunking.title import chunk_by_title

from langchain_core.documents import Document

from src.utils import (
    _log_elements_analysis,
    _get_element_type,
    _element_has_image_payload,
    _table_html,
    _create_table_description,
    _create_image_description,
    _stable_id,
    _get_page_number,
    _get_metadata_fields,
)

try:
    from .config import Config
except ImportError:
    from src.components.config import Config


# ── File-extension → partition function mapping ───────────────────────────
# Each value is either a callable(filename) or a dict of kwargs for richer calls.
# This replaces the long if/elif chain while keeping the exact same behaviour.

_PARTITION_MAP = {
    ".pdf": lambda f: partition_pdf(
        filename=f,
        strategy="hi_res",
        infer_table_structure=True,
        extract_image_block_types=["Image"],
        extract_image_block_to_payload=True,
    ),
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

    # ── Step 1: Parse raw file into unstructured elements ─────────────

    def process_documents(self, file_paths: str) -> List:
        """Parse a single file and return a list of unstructured elements."""
        path = Path(file_paths)
        extension = path.suffix.lower()
        file_name = path.name

        try:
            partition_fn = _PARTITION_MAP.get(extension)
            if partition_fn is None:
                raise ValueError(f"Unsupported file type: {extension}")

            elements = partition_fn(file_paths)
            print(f"Processed {file_paths} successfully. Extracted {len(elements)} elements.\n")

            _log_elements_analysis(elements)

            # Stamp source metadata onto every element
            for element in elements:
                if hasattr(element, "metadata"):
                    element.metadata.filename = file_name
                    element.metadata.filetype = extension.strip(".")
                    element.metadata.filepath = file_paths

            return elements

        except Exception as e:
            print(f"Error processing {file_paths}: {e}")
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
            print("Chunking TEXT elements by title...")
            text_chunks = chunk_by_title(
                elements=text_elements,
                max_characters=self.config.CHUNK_SIZE,
                new_after_n_chars=self.config.NEW_AFTER_N_CHARS,
                combine_text_under_n_chars=self.config.COMBINE_TEXT_UNDER_N_CHARS,
            )

            for i, chunk in enumerate(text_chunks, start=1):
                chunk_text = (chunk.text or "").strip()
                if len(chunk_text) < 10:
                    continue

                meta = _get_metadata_fields(chunk, _BASE_META_FIELDS + ["page_number"])
                meta.update({
                    "chunk_type": "text",
                    "source": meta["filepath"] or meta["filename"],
                    "chunk_index": i,
                    "chunk_id": _stable_id(
                        file_path=str(meta["filepath"] or "unknown"),
                        chunk_type="text",
                        index=i,
                        text=chunk_text,
                    ),
                })
                docs.append(Document(page_content=chunk_text, metadata=meta))

            print(f"Created {len(text_chunks)} TEXT chunks.")

        # ── Table chunks ─────────────────────────────────────────────
        if table_elements:
            print("Creating TABLE chunks...")

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
                    "chunk_id": _stable_id(
                        file_path=str(meta["filepath"] or "unknown"),
                        chunk_type="table",
                        index=i,
                        text=table_text,
                    ),
                })
                docs.append(Document(page_content=table_text, metadata=meta))

            print(f"Created {len(table_elements)} TABLE chunks.")

        # ── Image chunks ─────────────────────────────────────────────
        if image_elements:
            print("Creating IMAGE chunks...")

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
                    "chunk_id": _stable_id(
                        file_path=str(meta["filepath"] or "unknown"),
                        chunk_type="image",
                        index=i,
                        text=image_text,
                    ),
                })
                docs.append(Document(page_content=image_text, metadata=meta))

            print(f"Created {len(image_elements)} IMAGE chunks.")

        print(f"Total LangChain Documents created: {len(docs)}")
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