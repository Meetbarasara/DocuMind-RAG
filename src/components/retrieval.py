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
        # BUG-4/5 fix: start "dirty" so the first hybrid search rebuilds the
        # index from Pinecone instead of staying empty until an upload
        # happens to occur in *this* process — see _ensure_bm25_index.
        self._bm25_dirty = True

        # ── Feature B: Cross-Encoder Re-ranking (lazy-loaded) ────────────
        self._cross_encoder = None

    # ── Feature A: BM25 Index Management ──────────────────────────────────

    def invalidate_bm25_index(self) -> None:
        """Mark the BM25 index stale so it's rebuilt from Pinecone on next use.

        BUG-4/5 fix: the index used to be populated directly from whichever
        documents were just uploaded — overwritten (not accumulated) on
        each call, and only ever present in the one process that handled
        that specific upload. Call this after any ingest/delete; the next
        hybrid search lazily rebuilds the *entire* index straight from
        Pinecone (the real source of truth for "every chunk in this
        namespace"), so it's always complete and correct regardless of
        which process or worker handled the original upload.
        """
        self._bm25_dirty = True

    def _ensure_bm25_index(self) -> None:
        """Lazily (re)build the BM25 index from everything in Pinecone.

        No-op if the index isn't marked dirty. Fetches the full namespace
        via the same "dummy query, large k" pattern already used by
        delete_document_by_filename, since Pinecone doesn't otherwise
        expose a plain "list everything" call through this client.
        """
        if not self._bm25_dirty:
            return

        try:
            from langchain_community.retrievers import BM25Retriever

            all_docs = self.vectorstore.similarity_search(
                query="", k=10_000, filter=None,
            )
            self._bm25_docs = all_docs
            self._bm25_retriever = (
                BM25Retriever.from_documents(all_docs, k=self.config.TOP_K)
                if all_docs else None
            )
            self._bm25_dirty = False
            logger.info("BM25 index rebuilt from Pinecone: %d documents", len(all_docs))
        except ImportError:
            logger.warning(
                "langchain_community not installed — BM25 hybrid search disabled. "
                "Install with: pip install langchain-community rank_bm25"
            )
            self._bm25_retriever = None
            self._bm25_dirty = False
        except Exception as e:
            # Leave _bm25_dirty=True so the next query retries the rebuild
            # instead of silently running dense-only forever on a blip.
            logger.warning("BM25 index rebuild failed, using dense only this query: %s", e)
            self._bm25_retriever = None

    # ── Feature B: Cross-Encoder Re-ranking ───────────────────────────────

    def _get_cross_encoder(self):
        """Load the cross-encoder model synchronously (call from a thread).

        This is intentionally synchronous — callers must use
        ``asyncio.to_thread(_get_cross_encoder)`` or call it from a
        ``run_in_executor`` context to avoid blocking the event loop.
        The ~22MB model download/load takes ~1-3s on first call.
        """
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

    async def preload_cross_encoder(self):
        """Async-safe wrapper to pre-load the cross-encoder at startup.

        Called from the FastAPI lifespan so the first query doesn't pay
        the model-load cost. Uses asyncio.to_thread to keep the event loop free.
        """
        import asyncio
        await asyncio.to_thread(self._get_cross_encoder)
        logger.info("Cross-encoder pre-loaded asynchronously.")


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
        the higher-ranked chunk is kept.

        Logical Mistake #4 fix: this used to keep whichever chunk had more
        characters, which has no relationship to relevance. ``docs`` arrives
        here already ordered best-first by the caller (RRF score for hybrid
        search, similarity score for dense-only) and *before* re-ranking, so
        position in the list is the only relevance signal available at this
        point -- a real one, unlike length. Since the inner loop only ever
        compares index ``i`` against later indices ``j > i``, ``i`` is
        always the higher-ranked (or equally-ranked) one of any duplicate
        pair, so the fix is simply: always remove ``j``, never ``i``.
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
                    removed_indices.add(j)

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

        if not self.config.USE_HYBRID_SEARCH:
            return dense_docs

        # BUG-4/5 fix: rebuild from Pinecone if stale (first use, or after
        # an ingest/delete called invalidate_bm25_index()) instead of
        # relying solely on whatever update_bm25_index() happened to be
        # called with in this process.
        self._ensure_bm25_index()
        if self._bm25_retriever is None:
            return dense_docs

        # Run BM25 search
        try:
            bm25_docs = self._bm25_retriever.invoke(query)

            # Logical Mistake #3 fix: BM25Retriever.invoke() (rank_bm25's
            # get_top_n) is a plain argsort over the whole corpus and always
            # returns exactly k docs, even ones with zero term overlap with
            # the query, in a small namespace. RRF below fuses by *rank*,
            # not score, so those zero-relevance docs would otherwise ride
            # into the merged result with no quality check at all.
            # SIMILARITY_THRESHOLD doesn't apply here -- it's calibrated for
            # cosine similarity, a different scale than BM25's raw score --
            # so the scale-appropriate gate is BM25's own score: drop
            # anything that didn't actually match a single term.
            processed_query = self._bm25_retriever.preprocess_func(query)
            bm25_scores = self._bm25_retriever.vectorizer.get_scores(processed_query)
            score_by_doc_id = {
                id(d): s for d, s in zip(self._bm25_retriever.docs, bm25_scores)
            }
            bm25_docs = [d for d in bm25_docs if score_by_doc_id.get(id(d), 0) > 0]

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

    def retrieve_candidates(self, query: str, filename_filter: str = None) -> List[Document]:
        """Hybrid search + dedup, deliberately WITHOUT re-ranking (BUG-6).

        Used by multi-query retrieval so each sub-query contributes its
        full candidate set to a global merge across all sub-queries; the
        caller re-ranks once over that merged pool via :meth:`rerank`.
        Calling :meth:`retrieve` (which reranks and truncates to
        ``RERANKER_TOP_K`` *per sub-query*) here would throw away
        candidates before the merge ever happens and run the cross-encoder
        N times instead of once.

        Args:
            query:           The user's question or search string.
            filename_filter: If set, only return chunks from this filename.

        Returns:
            List of ``Document`` objects, de-duplicated but not re-ranked.
        """
        try:
            docs = self._hybrid_retrieve(query, filename_filter)
            logger.info(
                "Retrieved %d docs above threshold (%.2f)",
                len(docs), self.config.SIMILARITY_THRESHOLD,
            )
            return self._deduplicate_chunks(docs)
        except Exception as e:
            logger.error("Retrieval failed: %s", e)
            return []

    def rerank(self, query: str, docs: List[Document]) -> List[Document]:
        """Cross-encoder re-rank *docs* against *query* (Feature B).

        Public entry point for :meth:`_rerank_documents` — exposed
        separately from :meth:`retrieve` so multi-query retrieval can run
        it once over a merged candidate pool (BUG-6).
        """
        return self._rerank_documents(query, docs)

    def retrieve(self, query: str, filename_filter: str = None) -> List[Document]:
        """Search Pinecone for documents similar to *query* (single query).

        Pipeline: Hybrid Search (A) → Chunk Dedup (E) → Re-rank (B)

        Args:
            query:           The user's question or search string.
            filename_filter: If set, only return chunks from this filename.

        Returns:
            List of ``Document`` objects, de-duplicated and re-ranked.
        """
        return self.rerank(query, self.retrieve_candidates(query, filename_filter))

    # ── Deletion ──────────────────────────────────────────────────────────

    def delete_document_by_filename(self, filename: str):
        """Delete all vectors in Pinecone whose ``filename`` metadata matches.

        Bug 4 fix: Pinecone serverless indexes do NOT support filter-based
        deletion (``delete(filter={...})``). That call silently succeeds but
        deletes nothing on serverless. Vectors must be deleted by explicit ID.

        BUG-7 fix: the previous way of finding those IDs was
        ``similarity_search(query="", k=10_000, filter={"filename": ...})``
        — an embedded empty string fed into a *ranked* top-k vector search.
        That's not a guaranteed exhaustive enumeration of every matching
        vector (ANN search can have imperfect recall regardless of how
        large k is set), so a document with enough chunks — or just an
        unlucky day — could leave orphaned vectors behind.
        ``index.list(prefix=...)`` is a real, paginated *listing* of every
        vector ID with that prefix, not a search — relies on chunk_id
        being ``f"{filename}::{content_hash}"`` (see embeddings.py) so the
        filename forms a stable, listable ID prefix.
        """
        try:
            prefix = f"{filename}::"
            vector_ids = []
            for id_batch in self.vectorstore.index.list(
                prefix=prefix, namespace=self.config.PINECONE_NAMESPACE
            ):
                vector_ids.extend(id_batch)

            if not vector_ids:
                logger.warning(
                    "delete_document_by_filename: no vectors found with prefix=%s "
                    "(nothing deleted — file may not be indexed yet)",
                    prefix,
                )
                return

            self.vectorstore.delete(ids=vector_ids)
            logger.info(
                "Deleted %d vectors for filename=%s", len(vector_ids), filename
            )

        except Exception as e:
            logger.error(
                "Failed to delete documents with filename %s: %s", filename, e
            )


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