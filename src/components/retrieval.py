import hashlib
import os
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore

from src.components.config import Config
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  RetrievalManager — similarity search against Pinecone with score filtering,
#  hybrid BM25 search, re-ranking, and chunk deduplication
# ══════════════════════════════════════════════════════════════════════════════


class RetrievalManager:
    """Retrieve relevant documents from a Pinecone vector store.

    Enhanced with:
        - **Hybrid Search** (Feature A): BM25 keyword + dense vector search
        - **Re-ranking** (Feature B): Cross-encoder re-ranking of retrieved docs
        - **Chunk Dedup** (Feature E): Near-duplicate removal via Jaccard similarity
    """

    def __init__(self, config: Config):
        self.config = config

        # Expose the Pinecone key so the SDK can authenticate
        if self.config.PINECONE_API_KEY:
            os.environ["PINECONE_API_KEY"] = self.config.PINECONE_API_KEY

        # Connect to the existing Pinecone index
        self.vectorstore = PineconeVectorStore(
            index_name=self.config.PINECONE_INDEX_NAME,
            embedding=OpenAIEmbeddings(
                model=self.config.EMBEDDING_MODEL_NAME,
                openai_api_key=self.config.OPENAI_API_KEY,
            ),
            namespace=self.config.PINECONE_NAMESPACE,
        )

        # ── Feature A: BM25 Hybrid Search ────────────────────────────────
        self._bm25_retriever = None
        self._bm25_docs: List[Document] = []

        # ── Feature B: Cross-Encoder Re-ranking (lazy-loaded) ────────────
        self._cross_encoder = None

    # ── Feature A: BM25 Index Management ──────────────────────────────────

    def update_bm25_index(self, documents: List[Document]) -> None:
        """Rebuild the in-memory BM25 index from *documents*.

        Called after ingestion so hybrid search can combine keyword + dense
        retrieval. This is a lightweight in-memory index — no persistence.
        """
        if not documents:
            return

        try:
            from langchain_community.retrievers import BM25Retriever

            self._bm25_docs = documents
            self._bm25_retriever = BM25Retriever.from_documents(
                documents, k=self.config.TOP_K
            )
            logger.info("BM25 index rebuilt with %d documents", len(documents))
        except ImportError:
            logger.warning(
                "langchain_community not installed — BM25 hybrid search disabled. "
                "Install with: pip install langchain-community rank_bm25"
            )
            self._bm25_retriever = None

    # ── Feature B: Cross-Encoder Re-ranking ───────────────────────────────

    def _get_cross_encoder(self):
        """Lazy-load the cross-encoder model on first use (~22MB download)."""
        if self._cross_encoder is None:
            try:
                from sentence_transformers import CrossEncoder

                self._cross_encoder = CrossEncoder(self.config.RERANKER_MODEL)
                logger.info("Loaded cross-encoder: %s", self.config.RERANKER_MODEL)
            except ImportError:
                logger.warning(
                    "sentence-transformers not installed — re-ranking disabled. "
                    "Install with: pip install sentence-transformers"
                )
                return None
        return self._cross_encoder

    def _rerank_documents(
        self, query: str, docs: List[Document]
    ) -> List[Document]:
        """Re-rank *docs* by cross-encoder relevance to *query*.

        Returns the top ``config.RERANKER_TOP_K`` documents sorted by score.
        If the cross-encoder is unavailable, returns *docs* unchanged.
        """
        if not self.config.USE_RERANKING or not docs:
            return docs

        cross_encoder = self._get_cross_encoder()
        if cross_encoder is None:
            return docs

        # Score each (query, document) pair
        pairs = [(query, doc.page_content) for doc in docs]
        scores = cross_encoder.predict(pairs)

        # Sort by score descending, keep top-k
        scored_docs = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
        top_docs = [doc for doc, _ in scored_docs[: self.config.RERANKER_TOP_K]]

        logger.info(
            "Re-ranked %d → %d docs (model=%s)",
            len(docs), len(top_docs), self.config.RERANKER_MODEL,
        )
        return top_docs

    # ── Feature E: Chunk Overlap Deduplication ────────────────────────────

    @staticmethod
    def _jaccard_similarity(text_a: str, text_b: str) -> float:
        """Compute Jaccard similarity between two texts (word-level)."""
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def _deduplicate_chunks(self, docs: List[Document]) -> List[Document]:
        """Remove near-duplicate chunks using Jaccard similarity.

        When two chunks exceed ``config.CHUNK_DEDUP_THRESHOLD`` similarity,
        the longer chunk is kept (it likely contains more context).
        """
        if not self.config.USE_CHUNK_DEDUP or len(docs) <= 1:
            return docs

        threshold = self.config.CHUNK_DEDUP_THRESHOLD
        keep = []
        removed_indices = set()

        for i, doc_i in enumerate(docs):
            if i in removed_indices:
                continue

            for j in range(i + 1, len(docs)):
                if j in removed_indices:
                    continue

                sim = self._jaccard_similarity(
                    doc_i.page_content, docs[j].page_content
                )
                if sim >= threshold:
                    # Keep the longer chunk, mark the shorter for removal
                    if len(doc_i.page_content) >= len(docs[j].page_content):
                        removed_indices.add(j)
                    else:
                        removed_indices.add(i)
                        break  # doc_i is removed, stop comparing it

            if i not in removed_indices:
                keep.append(doc_i)

        if len(docs) != len(keep):
            logger.info(
                "Chunk dedup: %d → %d docs (threshold=%.2f)",
                len(docs), len(keep), threshold,
            )
        return keep

    # ── Core Retrieval ────────────────────────────────────────────────────

    def _dense_retrieve(
        self, query: str, filename_filter: str = None
    ) -> List[Document]:
        """Run dense (vector) similarity search against Pinecone."""
        filter_dict = {}
        if filename_filter:
            filter_dict["filename"] = filename_filter

        docs_and_scores = self.vectorstore.similarity_search_with_score(
            query,
            k=self.config.TOP_K,
            filter=filter_dict or None,
        )

        # Keep only results above the quality threshold
        threshold = self.config.SIMILARITY_THRESHOLD
        return [doc for doc, score in docs_and_scores if score >= threshold]

    def _hybrid_retrieve(
        self, query: str, filename_filter: str = None
    ) -> List[Document]:
        """Combine dense vector search with BM25 keyword search using RRF.

        Uses Reciprocal Rank Fusion to merge two ranked lists with
        configurable weight (``config.HYBRID_SEARCH_WEIGHT``).
        """
        # Always run dense search
        dense_docs = self._dense_retrieve(query, filename_filter)

        # If BM25 is not available or disabled, return dense only
        if not self.config.USE_HYBRID_SEARCH or self._bm25_retriever is None:
            return dense_docs

        # Run BM25 search
        try:
            bm25_docs = self._bm25_retriever.invoke(query)

            # Apply filename filter to BM25 results if needed
            if filename_filter:
                bm25_docs = [
                    d for d in bm25_docs
                    if d.metadata.get("filename") == filename_filter
                ]
        except Exception as e:
            logger.warning("BM25 retrieval failed, using dense only: %s", e)
            return dense_docs

        # ── Reciprocal Rank Fusion (RRF) ─────────────────────────────
        k = 60  # RRF constant
        weight_bm25 = self.config.HYBRID_SEARCH_WEIGHT
        weight_dense = 1.0 - weight_bm25

        # Score each doc by its rank in each list
        doc_scores: Dict[str, Tuple[float, Document]] = {}

        for rank, doc in enumerate(dense_docs):
            doc_id = hashlib.md5(doc.page_content.encode()).hexdigest()
            rrf_score = weight_dense / (k + rank + 1)
            doc_scores[doc_id] = (rrf_score, doc)

        for rank, doc in enumerate(bm25_docs):
            doc_id = hashlib.md5(doc.page_content.encode()).hexdigest()
            rrf_score = weight_bm25 / (k + rank + 1)
            if doc_id in doc_scores:
                existing_score, existing_doc = doc_scores[doc_id]
                doc_scores[doc_id] = (existing_score + rrf_score, existing_doc)
            else:
                doc_scores[doc_id] = (rrf_score, doc)

        # Sort by combined RRF score, return top-k
        sorted_docs = sorted(
            doc_scores.values(), key=lambda x: x[0], reverse=True
        )
        merged = [doc for _, doc in sorted_docs[: self.config.TOP_K]]

        logger.info(
            "Hybrid search: %d dense + %d BM25 → %d merged (weight=%.2f)",
            len(dense_docs), len(bm25_docs), len(merged),
            weight_bm25,
        )
        return merged

    # ── Public Retrieval API ──────────────────────────────────────────────

    def retrieve(self, query: str, filename_filter: str = None):
        """Search Pinecone for documents similar to *query*.

        Pipeline: Hybrid Search (A) → Chunk Dedup (E) → Re-rank (B)

        Args:
            query:           The user's question or search string.
            filename_filter: If set, only return chunks from this filename.

        Returns:
            List of ``Document`` objects, de-duplicated and re-ranked.
        """
        try:
            # Step 1: Retrieve (hybrid or dense-only)
            docs = self._hybrid_retrieve(query, filename_filter)

            logger.info(
                "Retrieved %d docs above threshold (%.2f)",
                len(docs), self.config.SIMILARITY_THRESHOLD,
            )

            # Step 2: Deduplicate near-overlapping chunks (Feature E)
            docs = self._deduplicate_chunks(docs)

            # Step 3: Re-rank with cross-encoder (Feature B)
            docs = self._rerank_documents(query, docs)

            return docs

        except Exception as e:
            logger.error("Retrieval failed: %s", e)
            return []

    # ── Deletion ──────────────────────────────────────────────────────────

    def delete_document_by_filename(self, filename: str):
        """Delete all vectors in Pinecone whose ``filename`` metadata matches."""
        try:
            self.vectorstore.delete(filter={"filename": filename})
            logger.info("Deleted documents with filename: %s", filename)
        except Exception as e:
            logger.error("Failed to delete documents with filename %s: %s", filename, e)


# ══════════════════════════════════════════════════════════════════════════════
#  Quick test — run three queries against the Smart Signal PDF
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    config = Config()
    retrieval = RetrievalManager(config)

    test_queries = [
        "What is Smart Signal?",
        "How does reinforcement learning work in traffic control?",
        "What object detection model is used?",
    ]

    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"QUERY: {query}")
        print(f"{'='*60}")
        docs = retrieval.retrieve(query)
        print(f"Retrieved {len(docs)} docs above threshold ({config.SIMILARITY_THRESHOLD})")
        for i, doc in enumerate(docs, 1):
            meta = doc.metadata
            print(f"\n  [{i}] File: {meta.get('filename','?')} | Page: {meta.get('page_number','?')} | Type: {meta.get('chunk_type','?')}")
            print(f"      Preview: {doc.page_content[:150]}...")
    print("\n✅ Retrieval test complete!")