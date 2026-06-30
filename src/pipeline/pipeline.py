"""pipeline.py — End-to-end RAG orchestrator for DocuMind.

Wires together:
    ingestion → embedding → retrieval → generation (→ optional evaluation)

Public API:
    RAGPipeline.ingest_file(file_path, user_id, namespace) → int  (chunk count)
    RAGPipeline.query(question, namespace, chat_history, filters) → dict
    RAGPipeline.ingest_and_query(file_path, question, ...) → dict
    RAGPipeline.delete_document(filename, namespace) → None
"""

import asyncio
import hashlib
import os
import time
import uuid
from collections import OrderedDict
from typing import Dict, List, Optional

from langsmith import traceable

from src.components.cache import QueryCache
from src.components.config import Config
from src.components.embeddings import EmbeddingManager
from src.components.generation import AnswerGeneration
from src.components.ingestion import DocumentProcessor
from src.components.retrieval import RetrievalManager
from src.exception import CustomException
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  RAGPipeline
# ══════════════════════════════════════════════════════════════════════════════


class RAGPipeline:
    """Orchestrates the full DocuMind RAG pipeline."""

    def __init__(self, config: Optional[Config] = None, db=None):
        self.config = config or Config()

        self.processor = DocumentProcessor(self.config)
        self.embedding_manager = EmbeddingManager(self.config)
        self.generation_manager = AnswerGeneration(self.config)
        # C1: exact-match query cache (Redis). Fail-open and disabled until
        # REDIS_URL is set, so it's a no-op by default.
        self.cache = QueryCache(self.config)
        # B-hybrid: SupabaseManager for storing/fetching page snapshots. None in
        # unit tests => page-image storage is simply skipped (graceful).
        self.db = db

        # RetrievalManager is created lazily (namespace can change per
        # request). Latency Optimization #6 fix: this used to be a plain
        # dict with no eviction, growing without bound as distinct
        # namespaces accumulate over the process's lifetime -- each entry
        # holds a vectorstore client and (once hybrid search runs) a full
        # BM25 corpus in RAM. An OrderedDict + move_to_end/popitem(last=False)
        # gives a simple LRU bound at config.MAX_CACHED_RETRIEVAL_MANAGERS.
        self._retrieval_managers: "OrderedDict[str, RetrievalManager]" = OrderedDict()

        logger.info("RAGPipeline initialised.")

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_retrieval_manager(self, namespace: str) -> RetrievalManager:
        """Return (and cache) a RetrievalManager for the given *namespace*."""
        # Nit fix: Pinecone treats namespace="" as a real, literal
        # namespace (the default one) — a caller that forgot to pass a
        # real per-user namespace would silently read/write that shared
        # bucket instead of failing loudly. Every query/ingest/delete path
        # goes through this one method, so this is the right chokepoint.
        if not namespace:
            raise CustomException(
                "Pinecone namespace must not be empty — refusing to silently "
                "fall back to the shared default namespace."
            )
        if namespace in self._retrieval_managers:
            self._retrieval_managers.move_to_end(namespace)  # mark as recently used
            return self._retrieval_managers[namespace]

        cfg = Config(
            PINECONE_NAMESPACE=namespace,
            GOOGLE_API_KEY=self.config.GOOGLE_API_KEY,
            PINECONE_API_KEY=self.config.PINECONE_API_KEY,
            PINECONE_INDEX_NAME=self.config.PINECONE_INDEX_NAME,
            EMBEDDING_MODEL_NAME=self.config.EMBEDDING_MODEL_NAME,
            TOP_K=self.config.TOP_K,
            SIMILARITY_THRESHOLD=self.config.SIMILARITY_THRESHOLD,
        )
        self._retrieval_managers[namespace] = RetrievalManager(cfg)
        logger.debug("Created RetrievalManager for namespace=%s", namespace)

        if len(self._retrieval_managers) > self.config.MAX_CACHED_RETRIEVAL_MANAGERS:
            evicted_namespace, _ = self._retrieval_managers.popitem(last=False)
            logger.info(
                "Evicted RetrievalManager for namespace=%s (LRU cache full, max=%d)",
                evicted_namespace, self.config.MAX_CACHED_RETRIEVAL_MANAGERS,
            )
        return self._retrieval_managers[namespace]

    # ─────────────────────────────────────────────────────────────────────────
    #  Ingest
    # ─────────────────────────────────────────────────────────────────────────

    def ingest_file(
        self,
        file_path: str,
        user_id: str = "default",
        namespace: str = "",
    ) -> int:
        """Parse → chunk → embed → upsert a file into Pinecone.

        Args:
            file_path:  Absolute or relative path to the document.
            user_id:    User identifier (used in namespace strategy).
            namespace:  Pinecone namespace. Defaults to *user_id* if empty.

        Returns:
            Number of chunks upserted.

        Raises:
            CustomException: on any stage failure.
        """
        if not os.path.exists(file_path):
            raise CustomException(f"File not found: {file_path}")

        # Default namespace = user_id for per-user isolation
        effective_namespace = namespace or user_id

        try:
            logger.info("Ingesting: %s (namespace=%s)", file_path, effective_namespace)

            # ── Step 1: Parse & chunk ────────────────────────────────────
            elements = self.processor.process_documents(file_path)
            docs = self.processor.build_langchain_documents(elements)
            logger.info("Parsed %d chunks from %s", len(docs), file_path)

            if not docs:
                logger.warning("No chunks extracted from %s", file_path)
                return 0

            # ── Step 2: Embed & upsert ───────────────────────────────────
            # Bug 1 fix: pass namespace explicitly instead of mutating the shared
            # self.config object, which is a singleton shared across all requests.
            # Mutating it was a race condition: concurrent uploads could overwrite
            # each other's namespace, sending embeddings to the wrong user's index.
            self.embedding_manager.create_vector_store(docs, namespace=effective_namespace)
            logger.info(
                "Upserted %d chunks to Pinecone (namespace=%s)", len(docs), effective_namespace
            )

            # L1: native hybrid stores sparse vectors in Pinecone alongside the
            # dense ones, so there's no in-process index to invalidate on upload.

            # C3: this namespace's documents changed — drop its cached answers
            # so the next query can't be served a stale one.
            self.cache.invalidate(effective_namespace)

            # B-hybrid: persist the rendered page snapshots so the answer step
            # can hand them to the multimodal LLM. Best-effort — a storage
            # hiccup must not fail an already-successful ingest.
            self._store_page_images(elements, effective_namespace)

            return len(docs)

        except CustomException:
            raise
        except Exception as e:
            logger.exception("ingest_file failed for %s", file_path)
            raise CustomException(f"Ingestion failed: {e}") from e

    def _store_page_images(self, parsed: dict, namespace: str) -> None:
        """Upload rendered page snapshots to Supabase (B-hybrid, best-effort)."""
        if not (self.db and self.config.USE_IMAGE_ANSWERING):
            return
        page_images = parsed.get("visual_page_images") or {}
        filename = parsed.get("filename")
        stored = 0
        for page_no, png in page_images.items():
            try:
                self.db.upload_page_image(namespace, filename, page_no, png)
                stored += 1
            except Exception as e:
                logger.warning("Failed to store page snapshot p%s for %s: %s", page_no, filename, e)
        if stored:
            logger.info("Stored %d page snapshot(s) for %s", stored, filename)

    def _gather_page_images(self, docs, namespace: str) -> list:
        """B-hybrid: fetch base64 page snapshots for retrieved visual chunks.

        Capped at MAX_PAGE_IMAGES_PER_ANSWER, deduped by (filename, page), and a
        no-op when image answering is off or no storage is configured — so
        text-only answers never pay a storage round-trip.
        """
        if not (self.db and self.config.USE_IMAGE_ANSWERING):
            return []
        import base64

        cap = self.config.MAX_PAGE_IMAGES_PER_ANSWER
        seen, images = set(), []
        for d in docs:
            if not d.metadata.get("has_visual"):
                continue
            fname, page = d.metadata.get("filename"), d.metadata.get("page_number")
            if not fname or page is None or (fname, page) in seen:
                continue
            seen.add((fname, page))
            data = self.db.download_page_image(namespace, fname, page)
            if data:
                images.append(base64.b64encode(data).decode())
            if len(images) >= cap:
                break
        return images

    # ─────────────────────────────────────────────────────────────────────────
    #  Multi-Query Retrieval Helper (Feature C)
    # ─────────────────────────────────────────────────────────────────────────

    @traceable(run_type="retriever", name="retrieve")
    async def _multi_query_retrieve_async(
        self,
        rewritten_query: str,
        retrieval_manager,
        filename_filter=None,
        query_vector=None,
        ) -> list:
        # BUG-3 fix: generate_multi_queries is now async (it awaits the LLM
        # call instead of blocking on it) — await it here too.
        queries = await self.generation_manager.generate_multi_queries(rewritten_query)

        # Run all Pinecone searches in parallel — this alone saves 400-600ms
        # BUG-6 fix: retrieve_candidates() (not retrieve()) — no per-query
        # re-ranking/truncation here, so nothing is thrown away before the
        # merge below sees every sub-query's full candidate set.
        # L4: reuse the already-computed embedding only for the sub-query it
        # actually belongs to (the rewritten query); any extra multi-queries are
        # different text and still embed on demand.
        async def search_one(q):
            loop = asyncio.get_event_loop()
            vec = query_vector if q == rewritten_query else None
            return await loop.run_in_executor(
                None,
                lambda: retrieval_manager.retrieve_candidates(q, filename_filter=filename_filter, query_vector=vec)
                )

        results = await asyncio.gather(*[search_one(q) for q in queries])

        # Merge and deduplicate
        all_docs, seen = [], set()
        for docs in results:
            for doc in docs:
                h = hashlib.md5(doc.page_content.encode()).hexdigest()
                if h not in seen:
                    seen.add(h)
                    all_docs.append(doc)

        logger.info("Parallel multi-query: %d queries → %d unique docs", len(queries), len(all_docs))

        # BUG-6 fix: re-rank ONCE over the full merged pool (using the
        # original rewritten query, not any individual sub-query), instead
        # of each sub-query independently re-ranking+truncating before the
        # merge ever happened. Cuts the cross-encoder from N calls to 1 and
        # lets it choose from the complete candidate set. Off the event
        # loop via run_in_executor, same as the retrieval calls above —
        # the cross-encoder is a blocking CPU call.
        loop = asyncio.get_event_loop()
        reranked = await loop.run_in_executor(
            None, lambda: retrieval_manager.rerank(rewritten_query, all_docs)
        )
        return reranked


    async def query(
        self,
        question: str,
        namespace: str = "",
        chat_history=None,
        filename_filter=None,
    ) -> dict:
        """Retrieve → generate an answer to *question* (async).

        Pipeline: Rewrite → Multi-Query (C, parallel) → Hybrid Retrieve (A) →
                  Dedup (E) → Re-rank (B) → Generate → Verify Citations (D)
        """
        chat_history = chat_history or []

        # C1: serve repeats straight from Redis. Only when there's no chat
        # history — with history the same question text can mean different
        # things, so the raw question isn't a safe exact-match key.
        use_cache = not chat_history
        if use_cache:
            cached = self.cache.get(namespace, question, filename_filter)
            if cached:
                logger.info("Query cache HIT (namespace=%s)", namespace)
                return {**cached, "rewritten_query": question,
                        "namespace": namespace, "cached": True}

        # Step 1: Rewrite query (async — uses non-blocking memory summarization)
        rewritten = await self.generation_manager.rewrite_query(question, chat_history)

        # C2: semantic cache — a near-identical past question (cosine on the
        # query embedding) is served without retrieval or the LLM.
        query_vec = None
        if use_cache and self.config.USE_SEMANTIC_CACHE:
            query_vec = self.embedding_manager.embed_query(rewritten)
            sem = self.cache.get_semantic(namespace, query_vec, filename_filter)
            if sem:
                logger.info("Semantic cache HIT (namespace=%s)", namespace)
                return {**sem, "rewritten_query": rewritten,
                        "namespace": namespace, "cached": "semantic"}

        # Step 2: Multi-query parallel Pinecone lookups
        retrieval_manager = self._get_retrieval_manager(namespace)
        _t_retrieve = time.perf_counter()
        docs = await self._multi_query_retrieve_async(rewritten, retrieval_manager, filename_filter, query_vector=query_vec)
        _retrieve_ms = (time.perf_counter() - _t_retrieve) * 1000

        if not docs:
            return {
                "answer": "I couldn't find any relevant information in the uploaded documents.",
                "sources": [], "rewritten_query": rewritten,
                "num_sources_used": 0, "namespace": namespace,
            }

        # B-hybrid: attach rendered page image(s) for any retrieved visual chunk.
        page_images = self._gather_page_images(docs, namespace)

        # Step 3: Generate answer (awaited — async since memory summarization is async)
        _t_gen = time.perf_counter()
        result = await self.generation_manager.generate(
            rewritten, docs, chat_history, page_images=page_images
        )
        _gen_ms = (time.perf_counter() - _t_gen) * 1000
        result["rewritten_query"] = rewritten
        result["namespace"] = namespace

        if use_cache:
            value = {
                "answer": result["answer"],
                "sources": result["sources"],
                "num_sources_used": result["num_sources_used"],
                **({"citation_verification": result["citation_verification"]}
                   if "citation_verification" in result else {}),
            }
            self.cache.set(namespace, question, value, filename_filter)
            if query_vec is not None:
                self.cache.add_semantic(namespace, query_vec, value, filename_filter)

        logger.info(
            "Query answered with %d sources (ns=%s) | O3 timing: retrieve=%.0fms generate=%.0fms",
            result["num_sources_used"], namespace, _retrieve_ms, _gen_ms,
        )
        return result
    
    @staticmethod
    def _replay_cached_stream(cached: dict):
        """Yield a cached answer as the same SSE sequence a live stream would:
        sources → one token chunk → citation_verification → DONE."""
        import json
        yield f"data: {json.dumps({'type': 'sources', 'sources': cached.get('sources', [])})}\n\n"
        answer = cached.get("answer", "")
        if answer:
            yield f"data: {json.dumps({'type': 'token', 'content': answer})}\n\n"
        if "citation_verification" in cached:
            yield f"data: {json.dumps({'type': 'citation_verification', **cached['citation_verification']})}\n\n"
        yield "data: [DONE]\n\n"

    async def query_stream(
        self,
        question: str,
        namespace: str = "",
        chat_history: Optional[List] = None,
        filename_filter: Optional[str] = None,
    ):
        """Streaming variant of :meth:`query` — async generator yielding SSE events.

        Pipeline: Rewrite → Multi-Query (C, parallel) → Hybrid Retrieve (A) →
                  Dedup (E) → Re-rank (B) → Stream Generate → Verify (D)

        Yields:
            SSE ``data:`` strings for sources, tokens, citation_verification,
            and ``[DONE]``.
        """
        import json

        chat_history = chat_history or []
        use_cache = not chat_history

        try:
            # C1: replay a cached answer as a stream (only when no history).
            if use_cache:
                cached = self.cache.get(namespace, question, filename_filter)
                if cached:
                    logger.info("Query cache HIT (stream, namespace=%s)", namespace)
                    for event in self._replay_cached_stream(cached):
                        yield event
                    return

            rewritten = await self.generation_manager.rewrite_query(question, chat_history)

            # C2: semantic cache — replay a near-identical past answer as a stream.
            query_vec = None
            if use_cache and self.config.USE_SEMANTIC_CACHE:
                query_vec = self.embedding_manager.embed_query(rewritten)
                sem = self.cache.get_semantic(namespace, query_vec, filename_filter)
                if sem:
                    logger.info("Semantic cache HIT (stream, namespace=%s)", namespace)
                    for event in self._replay_cached_stream(sem):
                        yield event
                    return

            retrieval_manager = self._get_retrieval_manager(namespace)
            docs = await self._multi_query_retrieve_async(
                rewritten, retrieval_manager, filename_filter, query_vector=query_vec
            )

            if not docs:
                yield f"data: {json.dumps({'type': 'sources', 'sources': []})}\n\n"
                yield f"data: {json.dumps({'type': 'token', 'content': 'I could not find any relevant information in the uploaded documents.'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            # B-hybrid: attach page image(s) for retrieved visual chunks.
            page_images = self._gather_page_images(docs, namespace)
            # C1: capture the finished answer via a side channel so we can cache
            # it once the stream completes (no re-parsing of our own SSE).
            capture = {} if use_cache else None
            async for event in self.generation_manager.generate_stream(
                rewritten, docs, chat_history, capture=capture, page_images=page_images
            ):
                yield event

            if use_cache and capture and capture.get("answer"):
                value = {
                    "answer": capture["answer"],
                    "sources": capture.get("sources", []),
                    "num_sources_used": len(docs),
                    **({"citation_verification": capture["citation_verification"]}
                       if "citation_verification" in capture else {}),
                }
                self.cache.set(namespace, question, value, filename_filter)
                if query_vec is not None:
                    self.cache.add_semantic(namespace, query_vec, value, filename_filter)

        except Exception as e:
            # SEC-4: str(e) used to go straight into the SSE event sent to
            # the client — could leak provider error text or internal
            # details. Log the real error with a reference id instead.
            error_id = uuid.uuid4().hex[:8]
            logger.error("[%s] pipeline.query_stream failed: %s", error_id, e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': f'Something went wrong. (ref: {error_id})'})}\n\n"
            yield "data: [DONE]\n\n"

    # ─────────────────────────────────────────────────────────────────────────
    #  Delete
    # ─────────────────────────────────────────────────────────────────────────

    def delete_document(self, filename: str, namespace: str = "") -> None:
        """Remove all Pinecone vectors associated with *filename*.

        Args:
            filename:  The original filename stored in Pinecone metadata.
            namespace: Pinecone namespace where the vectors reside.
        """
        try:
            retrieval_manager = self._get_retrieval_manager(namespace)
            retrieval_manager.delete_document_by_filename(filename)
            # C3: drop this namespace's cached answers — a deleted doc must not
            # keep answering from cache.
            self.cache.invalidate(namespace)
            logger.info("Deleted Pinecone vectors for %s (namespace=%s)", filename, namespace)
        except Exception as e:
            logger.exception("delete_document failed for %s", filename)
            raise CustomException(f"Delete failed: {e}") from e

    # ─────────────────────────────────────────────────────────────────────────
    #  Convenience
    # ─────────────────────────────────────────────────────────────────────────

    async def ingest_and_query(
        self,
        file_path: str,
        question: str,
        user_id: str = "default",
        namespace: str = "",
        chat_history: Optional[List] = None,
    ) -> Dict:
        """Convenience: ingest a file then immediately answer a question.

        Returns the same dict as :meth:`query`, plus an ``"ingested_chunks"`` key.
        """
        # BUG-1 fix: same async/await mismatch as the chat routes — `query` is
        # `async def`, so calling it without `await` returned a coroutine and
        # `result["ingested_chunks"] = ...` would raise TypeError. This method
        # itself must be async too since it now awaits `self.query(...)`.
        effective_namespace = namespace or user_id
        chunk_count = self.ingest_file(file_path, user_id=user_id, namespace=effective_namespace)
        result = await self.query(
            question,
            namespace=effective_namespace,
            chat_history=chat_history,
        )
        result["ingested_chunks"] = chunk_count
        return result


# ══════════════════════════════════════════════════════════════════════════════
#  Quick smoke test
# ══════════════════════════════════════════════════════════════════════════════

async def _smoke_test():
    from pathlib import Path

    config = Config()
    pipeline = RAGPipeline(config)

    pdf_path = str(
        Path(__file__).parent.parent.parent
        / "docs"
        / "Smart_Signal__Adaptive_Traffic_Signal_Control_using_Reinforcement_Learning_and_Object_Detection.pdf"
    )

    print("\\n=== Ingesting PDF ===")
    chunks = pipeline.ingest_file(pdf_path, user_id="test_user")
    print(f"Ingested {chunks} chunks")

    print("\\n=== Querying ===")
    # BUG-1 fix: query() is async — must be awaited, same as the chat routes.
    result = await pipeline.query(
        "How does Smart Signal use reinforcement learning?",
        namespace="test_user",
    )
    print("Answer:", result["answer"])
    print("Sources:", result["sources"])


if __name__ == "__main__":
    asyncio.run(_smoke_test())