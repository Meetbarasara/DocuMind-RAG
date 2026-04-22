import hashlib
import os
from typing import List

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore

from src.components.config import Config
from src.components.ingestion import DocumentProcessor
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  EmbeddingManager — deduplicate, embed, and upsert documents to Pinecone
# ══════════════════════════════════════════════════════════════════════════════


class EmbeddingManager:
    """Handles embedding documents via OpenAI and upserting them into Pinecone."""

    def __init__(self, config: Config):
        self.config = config

        # Expose the Pinecone key so the SDK can authenticate
        if self.config.PINECONE_API_KEY:
            os.environ["PINECONE_API_KEY"] = self.config.PINECONE_API_KEY

    # ── Static helpers ────────────────────────────────────────────────────

    @staticmethod
    def hash_content(text: str) -> str:
        """Return a SHA-256 hex digest of *text* (used for deduplication)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def clean_metadata(metadata: dict) -> dict:
        """Strip ``None`` values and coerce non-primitive types to strings.

        Pinecone only accepts str / int / float / bool metadata values,
        so anything else (lists, dicts, etc.) is converted to its string
        representation.
        """
        cleaned = {}
        for key, value in metadata.items():
            if value is None:
                continue
            elif isinstance(value, (str, int, float, bool)):
                cleaned[key] = value
            else:
                cleaned[key] = str(value)
        return cleaned

    # ── Core method ───────────────────────────────────────────────────────

    def create_vector_store(
        self,
        documents: List[Document],
        persist_directory: str = None,
    ) -> PineconeVectorStore:
        """Deduplicate *documents*, embed them, and upsert into Pinecone.

        Args:
            documents: List of LangChain ``Document`` objects to embed.
            persist_directory: (unused — kept for API compatibility).

        Returns:
            A ``PineconeVectorStore`` instance pointing at the target index.
        """

        # ── Build the embedding model ────────────────────────────────────
        embedding_model = OpenAIEmbeddings(
            model=self.config.EMBEDDING_MODEL_NAME,
            openai_api_key=self.config.OPENAI_API_KEY,
        )

        # ── Early exit: nothing to embed ─────────────────────────────────
        if not documents:
            logger.info("No new documents to embed. Returning existing vector store.")
            return PineconeVectorStore(
                index_name=self.config.PINECONE_INDEX_NAME,
                embedding=embedding_model,
                namespace=self.config.PINECONE_NAMESPACE,
            )

        try:
            # ── Step 1: Deduplicate by content hash ──────────────────────
            unique_docs: List[Document] = []
            seen_hashes: set = set()

            for doc in documents:
                content_hash = self.hash_content(doc.page_content)

                if content_hash in seen_hashes:
                    continue

                seen_hashes.add(content_hash)
                doc.metadata["content_hash"] = content_hash
                unique_docs.append(doc)

            documents = unique_docs
            logger.info(
                "Deduplicated: %d unique docs (from %d total)",
                len(unique_docs),
                len(unique_docs) + len(seen_hashes) - len(unique_docs),
            )

            # ── Step 2: Assign a stable chunk_id per document ────────────
            for doc in documents:
                source = doc.metadata.get("source", "unknown")
                doc.metadata["chunk_id"] = f"{source}::{doc.metadata['content_hash']}"

            # ── Step 3: Clean metadata for Pinecone compatibility ────────
            for doc in documents:
                doc.metadata = self.clean_metadata(doc.metadata)

            # ── Step 4: Upsert into Pinecone ─────────────────────────────
            vector_store = PineconeVectorStore(
                index_name=self.config.PINECONE_INDEX_NAME,
                embedding=embedding_model,
                namespace=self.config.PINECONE_NAMESPACE,
            )

            chunk_ids = [doc.metadata["chunk_id"] for doc in documents]
            vector_store.add_documents(documents=documents, ids=chunk_ids)

            logger.info("Upserted %d documents to Pinecone.", len(documents))
            return vector_store

        except Exception:
            logger.exception(
                "Failed to create vector store (num_documents=%d)",
                len(documents),
            )
            raise


# ══════════════════════════════════════════════════════════════════════════════
#  Quick test — Ingest Smart Signal PDF → Embed → Upsert
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from pathlib import Path

    config = Config()

    # ── Step 1: Ingest the Smart Signal PDF ──────────────────────────────
    processor = DocumentProcessor(config)
    pdf_path = str(
        Path(__file__).parent.parent.parent
        / "docs"
        / "Smart_Signal__Adaptive_Traffic_Signal_Control_using_Reinforcement_Learning_and_Object_Detection.pdf"
    )
    print(f"\n{'='*60}")
    print(f"STEP 1: Parsing PDF → {pdf_path}")
    print(f"{'='*60}")
    elements = processor.process_documents(pdf_path)
    langchain_docs = processor.build_langchain_documents(elements)
    print(f"Total LangChain Documents: {len(langchain_docs)}")

    # ── Step 2: Embed & upsert to Pinecone ───────────────────────────────
    print(f"\n{'='*60}")
    print(f"STEP 2: Embedding {len(langchain_docs)} docs → Pinecone (index={config.PINECONE_INDEX_NAME})")
    print(f"{'='*60}")
    embedding_manager = EmbeddingManager(config)
    vector_store = embedding_manager.create_vector_store(langchain_docs)
    print("\n✅ Embedding pipeline complete!")