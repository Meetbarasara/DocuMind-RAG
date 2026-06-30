from typing import List

from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_pinecone import PineconeVectorStore

from src.components.config import Config
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  RetrievalManager — dense + native sparse hybrid search against Pinecone,
#  with score filtering, near-duplicate dedup, and Cohere re-ranking.
# ══════════════════════════════════════════════════════════════════════════════
#
# L1: hybrid is now Pinecone-*native*. Each chunk is stored with a DENSE vector
# (Gemini) + a SPARSE keyword vector (src/components/sparse.py) in one dotproduct
# index, and the query is fused server-side. This replaces the old in-process
# BM25 index — no per-process RAM index, no full-namespace rebuild on the first
# query after an upload, multi-worker/restart safe. Requires a dotproduct index;
# when USE_HYBRID_SEARCH is off (or a hybrid query errors) it falls back to plain
# dense search, which works on any index.


class RetrievalManager:
    """Retrieve relevant documents from a Pinecone vector store.

    Pipeline: dense (or native hybrid) search → near-duplicate dedup → Cohere re-rank.
    """

    def __init__(self, config: Config):
        self.config = config

        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=self.config.EMBEDDING_MODEL_NAME,
            google_api_key=self.config.GOOGLE_API_KEY,
            output_dimensionality=self.config.EMBEDDING_DIMENSIONS,
        )
        # A10: pass the key directly to the client instead of mutating the
        # process-global os.environ — a constructor shouldn't have that kind
        # of surprising, process-wide side effect.
        self.vectorstore = PineconeVectorStore(
            index_name=self.config.PINECONE_INDEX_NAME,
            embedding=self._embeddings,
            namespace=self.config.PINECONE_NAMESPACE,
            pinecone_api_key=self.config.PINECONE_API_KEY,
        )

        # Re-ranking via Cohere Rerank API (lazy client)
        self._cohere_client = None

    # ── Re-ranking via Cohere Rerank API (L2) ─────────────────────────────

    def _get_cohere_client(self):
        """Return a cached Cohere client, or None if reranking can't run.

        L2: replaced the local sentence-transformers CrossEncoder (heavy CPU +
        ~1GB torch) with Cohere's hosted Rerank API. Returns None when
        COHERE_API_KEY is unset or the SDK isn't installed so the caller can
        degrade gracefully instead of crashing.
        """
        if self._cohere_client is None and self.config.COHERE_API_KEY:
            try:
                import cohere

                self._cohere_client = cohere.ClientV2(api_key=self.config.COHERE_API_KEY)
                logger.info("Initialised Cohere rerank client (model=%s)", self.config.COHERE_RERANK_MODEL)
            except ImportError:
                logger.warning("cohere not installed — re-ranking disabled. pip install cohere")
                return None
        return self._cohere_client

    def _rerank_documents(self, query: str, docs: List[Document]) -> List[Document]:
        """Re-rank *docs* by Cohere relevance to *query*, keeping the top
        ``config.RERANKER_TOP_K``. Degrades to retrieval order if reranking is
        off, there are no docs, or no Cohere client is available."""
        if not self.config.USE_RERANKING or not docs:
            return docs

        top_k = self.config.RERANKER_TOP_K
        client = self._get_cohere_client()
        if client is None:
            logger.debug("Rerank skipped (no Cohere client); keeping top %d by retrieval order", top_k)
            return docs[:top_k]

        try:
            response = client.rerank(
                model=self.config.COHERE_RERANK_MODEL,
                query=query,
                documents=[doc.page_content for doc in docs],
                top_n=top_k,
            )
            reranked = [docs[result.index] for result in response.results]
            logger.info("Cohere re-ranked %d → %d docs", len(docs), len(reranked))
            return reranked
        except Exception as e:
            logger.warning("Cohere rerank failed, keeping top %d by retrieval order: %s", top_k, e)
            return docs[:top_k]

    # ── Chunk Overlap Deduplication ───────────────────────────────────────

    @staticmethod
    def _jaccard_similarity(text_a: str, text_b: str) -> float:
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)

    def _deduplicate_chunks(self, docs: List[Document]) -> List[Document]:
        """Remove near-duplicate chunks (Jaccard). ``docs`` is ordered best-first
        and dedup runs *before* re-rank, so for any duplicate pair the earlier
        (higher-ranked) index is kept and the later one dropped."""
        if not self.config.USE_CHUNK_DEDUP or len(docs) <= 1:
            return docs

        threshold = self.config.CHUNK_DEDUP_THRESHOLD
        keep, removed = [], set()
        for i, doc_i in enumerate(docs):
            if i in removed:
                continue
            for j in range(i + 1, len(docs)):
                if j in removed:
                    continue
                if self._jaccard_similarity(doc_i.page_content, docs[j].page_content) >= threshold:
                    removed.add(j)
            keep.append(doc_i)

        if len(docs) != len(keep):
            logger.info("Chunk dedup: %d → %d docs (threshold=%.2f)", len(docs), len(keep), threshold)
        return keep

    # ── Core Retrieval ────────────────────────────────────────────────────

    def _dense_retrieve(self, query: str, filename_filter: str = None, query_vector=None) -> List[Document]:
        """Dense (vector) similarity search against Pinecone, score-filtered.

        Works on any index; used directly when hybrid is off and as the fallback
        if a hybrid query fails.

        L4: if *query_vector* is supplied (already computed upstream for the C2
        semantic-cache lookup), search by that vector so we don't embed the same
        query text a second time. Falls back to text search (embed-on-demand)
        when no vector is given.
        """
        filter_dict = {"filename": filename_filter} if filename_filter else None
        if query_vector is not None:
            docs_and_scores = self.vectorstore.similarity_search_by_vector_with_score(
                query_vector, k=self.config.TOP_K, filter=filter_dict,
            )
        else:
            docs_and_scores = self.vectorstore.similarity_search_with_score(
                query, k=self.config.TOP_K, filter=filter_dict,
            )
        threshold = self.config.SIMILARITY_THRESHOLD
        return [doc for doc, score in docs_and_scores if score >= threshold]

    def _hybrid_retrieve(self, query: str, filename_filter: str = None, query_vector=None) -> List[Document]:
        """Pinecone *native* sparse+dense hybrid query (L1).

        Embeds the query (dense) + sparse-encodes it, convex-weights the two by
        ``config.HYBRID_ALPHA``, and runs a single server-side fused query
        against the dotproduct index. Falls back to dense search on any error
        (e.g. the index isn't dotproduct).

        L4: reuse *query_vector* for the dense half when it was already computed
        upstream (C2), instead of embedding the query text again here.
        """
        if not self.config.USE_HYBRID_SEARCH:
            return self._dense_retrieve(query, filename_filter, query_vector)

        from src.components.sparse import convex_scale, encode_text

        try:
            dense = query_vector if query_vector is not None else self._embeddings.embed_query(query)
            scaled_dense, scaled_sparse = convex_scale(
                dense, encode_text(query), self.config.HYBRID_ALPHA
            )
            kwargs = dict(
                vector=scaled_dense,
                top_k=self.config.TOP_K,
                include_metadata=True,
                namespace=self.config.PINECONE_NAMESPACE,
            )
            if scaled_sparse["indices"]:
                kwargs["sparse_vector"] = scaled_sparse
            if filename_filter:
                kwargs["filter"] = {"filename": filename_filter}

            result = self.vectorstore.index.query(**kwargs)
            text_key = getattr(self.vectorstore, "_text_key", "text")
            docs = []
            for match in result.matches:
                meta = dict(match.metadata or {})
                docs.append(Document(page_content=meta.pop(text_key, ""), metadata=meta))
            logger.info("Native hybrid query: %d matches (alpha=%.2f)", len(docs), self.config.HYBRID_ALPHA)
            return docs
        except Exception as e:
            logger.warning("Hybrid query failed, falling back to dense: %s", e)
            return self._dense_retrieve(query, filename_filter, query_vector)

    # ── Public Retrieval API ──────────────────────────────────────────────

    def retrieve_candidates(self, query: str, filename_filter: str = None, query_vector=None) -> List[Document]:
        """Search + dedup, deliberately WITHOUT re-ranking (BUG-6).

        Multi-query retrieval merges each sub-query's full candidate set, then
        re-ranks once over the union via :meth:`rerank`.

        L4: *query_vector* (when set) is the embedding already computed upstream
        and is reused instead of re-embedding *query*.
        """
        try:
            docs = self._deduplicate_chunks(self._hybrid_retrieve(query, filename_filter, query_vector))
            logger.info("Retrieved %d candidate docs (threshold=%.2f)", len(docs), self.config.SIMILARITY_THRESHOLD)
            return docs
        except Exception as e:
            logger.error("Retrieval failed: %s", e)
            return []

    def rerank(self, query: str, docs: List[Document]) -> List[Document]:
        """Cohere re-rank *docs* — exposed separately from :meth:`retrieve` so
        multi-query retrieval can rerank once over the merged pool (BUG-6)."""
        return self._rerank_documents(query, docs)

    def retrieve(self, query: str, filename_filter: str = None, query_vector=None) -> List[Document]:
        """Single-query retrieval: Search → Dedup → Cohere Re-rank."""
        return self.rerank(query, self.retrieve_candidates(query, filename_filter, query_vector))

    # ── Deletion ──────────────────────────────────────────────────────────

    def delete_document_by_filename(self, filename: str):
        """Delete all vectors in Pinecone whose ``chunk_id`` starts with
        ``{filename}::`` (BUG-7: a real paginated id listing, not a ranked
        search). Serverless can't filter-delete, so delete by explicit id."""
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
                    "(nothing deleted — file may not be indexed yet)", prefix,
                )
                return

            self.vectorstore.delete(ids=vector_ids)
            logger.info("Deleted %d vectors for filename=%s", len(vector_ids), filename)
        except Exception as e:
            logger.error("Failed to delete documents with filename %s: %s", filename, e)
