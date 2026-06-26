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

        # Latency Optimization #5 fix: this used to be rebuilt inside
        # create_vector_store on every call -- i.e. once per file upload --
        # even though the model name and API key never change. Building it
        # once here and reusing it avoids paying the underlying HTTP
        # client's setup cost on every single upload.
        self._embedding_model = OpenAIEmbeddings(
            model=self.config.EMBEDDING_MODEL_NAME,
            openai_api_key=self.config.OPENAI_API_KEY,
        )

    def embed_query(self, text: str) -> list:
        """Embed a single query string (reuses the shared embedding model)."""
        return self._embedding_model.embed_query(text)

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
        namespace: str = None,
    ) -> PineconeVectorStore:
        """Deduplicate *documents*, embed them, and upsert into Pinecone.

        Args:
            documents:         List of LangChain ``Document`` objects to embed.
            persist_directory: (unused — kept for API compatibility).
            namespace:         Pinecone namespace to upsert into. When provided,
                               this takes priority over ``config.PINECONE_NAMESPACE``.
                               Always pass this explicitly to avoid race conditions
                               when the pipeline is used as a shared singleton.

        Returns:
            A ``PineconeVectorStore`` instance pointing at the target index.
        """
        # Resolve namespace: explicit arg wins, then fall back to config
        effective_namespace = namespace if namespace is not None else self.config.PINECONE_NAMESPACE

        # Latency Optimization #5 fix: reuse the embedding model built once
        # in __init__ instead of constructing a new one on every call.
        embedding_model = self._embedding_model

        # ── Early exit: nothing to embed ─────────────────────────────────
        if not documents:
            logger.info("No new documents to embed. Returning existing vector store.")
            return PineconeVectorStore(
                index_name=self.config.PINECONE_INDEX_NAME,
                embedding=embedding_model,
                namespace=effective_namespace,
            )

        try:
            # ── Step 1: Deduplicate by content hash ──────────────────────
            total_input = len(documents)
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
                total_input,
            )

            # ── Step 2: Assign a stable chunk_id per document ────────────
            # BUG-7 fix: this used metadata["source"] (the local temp
            # filesystem path used during ingestion, e.g.
            # "/tmp_uploads/report.pdf") instead of metadata["filename"]
            # (the stable, sanitized original name). The temp path isn't a
            # reliable prefix to delete by later — using "filename" instead
            # makes chunk_id a stable f"{filename}::{hash}" id, so every
            # chunk of a given file can be exhaustively listed by prefix
            # (see RetrievalManager.delete_document_by_filename).
            for doc in documents:
                file_key = doc.metadata.get("filename", "unknown")
                doc.metadata["chunk_id"] = f"{file_key}::{doc.metadata['content_hash']}"

            # ── Step 3: Clean metadata for Pinecone compatibility ────────
            for doc in documents:
                doc.metadata = self.clean_metadata(doc.metadata)

            # ── Step 4: Upsert into Pinecone ─────────────────────────────
            vector_store = PineconeVectorStore(
                index_name=self.config.PINECONE_INDEX_NAME,
                embedding=embedding_model,
                namespace=effective_namespace,
            )

            chunk_ids = [doc.metadata["chunk_id"] for doc in documents]
            if self.config.USE_HYBRID_SEARCH:
                # L1: native hybrid — store a dense + sparse vector per chunk so
                # Pinecone fuses them server-side (requires a dotproduct index).
                self._upsert_hybrid(documents, chunk_ids, vector_store, effective_namespace)
            else:
                vector_store.add_documents(documents=documents, ids=chunk_ids)

            logger.info("Upserted %d documents to Pinecone.", len(documents))
            return vector_store

        except Exception:
            logger.exception(
                "Failed to create vector store (num_documents=%d)",
                len(documents),
            )
            raise

    def _upsert_hybrid(self, documents, chunk_ids, vector_store, namespace) -> None:
        """L1: upsert a dense + sparse vector per chunk for Pinecone native hybrid.

        Needs a dotproduct index. The sparse vector is the stateless lexical
        encoding from src/components/sparse.py; the dense vector is the same
        OpenAI embedding used everywhere else. page_content is stored under the
        vectorstore's text key so retrieval reconstructs Documents the same way.
        """
        from src.components.sparse import encode_text

        text_key = getattr(vector_store, "_text_key", "text")
        dense_vectors = self._embedding_model.embed_documents([d.page_content for d in documents])

        vectors = []
        for doc, vid, dense in zip(documents, chunk_ids, dense_vectors):
            meta = dict(doc.metadata)
            meta[text_key] = doc.page_content
            vector = {"id": vid, "values": dense, "metadata": meta}
            sparse = encode_text(doc.page_content)
            if sparse["indices"]:
                vector["sparse_values"] = sparse
            vectors.append(vector)

        index = vector_store.index
        batch = self.config.EMBEDDING_BATCH_SIZE
        for start in range(0, len(vectors), batch):
            index.upsert(vectors=vectors[start : start + batch], namespace=namespace)


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