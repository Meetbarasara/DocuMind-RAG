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
import os
import uuid
from typing import Dict, List, Optional

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

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

        self.processor = DocumentProcessor(self.config)
        self.embedding_manager = EmbeddingManager(self.config)
        self.generation_manager = AnswerGeneration(self.config)

        # RetrievalManager is created lazily (namespace can change per request)
        self._retrieval_managers: Dict[str, RetrievalManager] = {}

        logger.info("RAGPipeline initialised.")

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_retrieval_manager(self, namespace: str) -> RetrievalManager:
        """Return (and cache) a RetrievalManager for the given *namespace*."""
        if namespace not in self._retrieval_managers:
            cfg = Config(
                PINECONE_NAMESPACE=namespace,
                OPENAI_API_KEY=self.config.OPENAI_API_KEY,
                PINECONE_API_KEY=self.config.PINECONE_API_KEY,
                PINECONE_INDEX_NAME=self.config.PINECONE_INDEX_NAME,
                EMBEDDING_MODEL_NAME=self.config.EMBEDDING_MODEL_NAME,
                TOP_K=self.config.TOP_K,
                SIMILARITY_THRESHOLD=self.config.SIMILARITY_THRESHOLD,
            )
            self._retrieval_managers[namespace] = RetrievalManager(cfg)
            logger.debug("Created RetrievalManager for namespace=%s", namespace)
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

            # ── Step 3: Update BM25 index for hybrid search ──────────────
            # Bug 4 fix: the BM25 index was never populated after ingestion,
            # so hybrid search always fell back silently to dense-only retrieval.
            # Now we rebuild the index on the correct per-namespace manager.
            if self.config.USE_HYBRID_SEARCH:
                retrieval_manager = self._get_retrieval_manager(effective_namespace)
                retrieval_manager.update_bm25_index(docs)
                logger.info(
                    "BM25 index updated for namespace=%s (%d docs)",
                    effective_namespace, len(docs),
                )

            return len(docs)

        except CustomException:
            raise
        except Exception as e:
            logger.exception("ingest_file failed for %s", file_path)
            raise CustomException(f"Ingestion failed: {e}") from e

    # ─────────────────────────────────────────────────────────────────────────
    #  Multi-Query Retrieval Helper (Feature C)
    # ─────────────────────────────────────────────────────────────────────────

    async def _multi_query_retrieve_async(
        self,
        rewritten_query: str,
        retrieval_manager,
        filename_filter=None,
        ) -> list:
        import asyncio, hashlib as _hashlib

        # BUG-3 fix: generate_multi_queries is now async (it awaits the LLM
        # call instead of blocking on it) — await it here too.
        queries = await self.generation_manager.generate_multi_queries(rewritten_query)

        # Run all Pinecone searches in parallel — this alone saves 400-600ms
        async def search_one(q):
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: retrieval_manager.retrieve(q, filename_filter=filename_filter)
                )

        results = await asyncio.gather(*[search_one(q) for q in queries])

        # Merge and deduplicate
        all_docs, seen = [], set()
        for docs in results:
            for doc in docs:
                h = _hashlib.md5(doc.page_content.encode()).hexdigest()
                if h not in seen:
                    seen.add(h)
                    all_docs.append(doc)

        logger.info("Parallel multi-query: %d queries → %d unique docs", len(queries), len(all_docs))
        return all_docs


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

        retrieval_manager = self._get_retrieval_manager(namespace)

        # Step 1: Rewrite query (async — uses non-blocking memory summarization)
        rewritten = await self.generation_manager.rewrite_query(question, chat_history)

        # Step 2: Multi-query parallel Pinecone lookups
        docs = await self._multi_query_retrieve_async(rewritten, retrieval_manager, filename_filter)

        if not docs:
            return {
                "answer": "I couldn't find any relevant information in the uploaded documents.",
                "sources": [], "rewritten_query": rewritten,
                "num_sources_used": 0, "namespace": namespace,
            }

        # Step 3: Generate answer (awaited — async since memory summarization is async)
        result = await self.generation_manager.generate(rewritten, docs, chat_history)
        result["rewritten_query"] = rewritten
        result["namespace"] = namespace

        logger.info(
            "Query answered with %d sources (namespace=%s)",
            result["num_sources_used"], namespace,
        )
        return result
    
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

        try:
            rewritten = await self.generation_manager.rewrite_query(question, chat_history)
            retrieval_manager = self._get_retrieval_manager(namespace)
            docs = await self._multi_query_retrieve_async(
                rewritten, retrieval_manager, filename_filter
            )

            if not docs:
                yield f"data: {json.dumps({'type': 'sources', 'sources': []})}\n\n"
                yield f"data: {json.dumps({'type': 'token', 'content': 'I could not find any relevant information in the uploaded documents.'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            async for event in self.generation_manager.generate_stream(rewritten, docs, chat_history):
                yield event

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