import json
import logging
import re
from contextlib import nullcontext
from typing import Any, Dict, List

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tracers.context import collect_runs
from langchain_groq import ChatGroq

from src.components.config import Config
from src.components.retrieval import RetrievalManager
from src.logger import get_logger
from src.utils import format_chat_history_async

logger = get_logger(__name__)

# Silence noisy httpx logs (emitted on every Groq API call)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
#  Prompt templates
# ══════════════════════════════════════════════════════════════════════════════

_RAG_SYSTEM_PROMPT = """\
You are a precise assistant that answers questions strictly from the provided document context.

Rules:
1. Use ONLY the information in the context below. Do not add facts, assumptions, or outside/general knowledge — every statement you make must be directly supported by the context.
2. If the answer is not in the context, reply exactly: "I cannot find information about your question from the document".
3. Answer directly and lead with the answer — no preamble (do NOT start with phrases like "Based on the provided context" or "According to the documents"). Include only what the question asks for; do not pad.
4. Cite each supporting fact inline using the [Source: filename, Page: X] format.
5. Use the conversation history only to interpret follow-up questions, never as a source of facts.

Conversation History:
{chat_history}

Context:
{context}
"""

_REWRITE_SYSTEM_PROMPT = """\
You are an AI assistant that rewrites conversational follow-up questions into standalone search queries.
Given the conversation history, rephrase the follow-up question to be a standalone query that can be used to search a document database.
- Resolve any pronouns (it, they, them, this, these, those) to their specific referents from the history.
- If the user uses vague terms like "all 3" or "both", explicitly list what those refer to based on the previous messages.
- Do not answer the question, ONLY return the rewritten standalone question.
- If the question is already completely standalone, return it exactly as is.

Conversation History:
{chat_history}"""

_MULTI_QUERY_PROMPT = """\
You are an AI assistant that generates diverse search queries for a document retrieval system.
Given the original query, generate {count} different reformulations that capture the same intent
from different angles. Each reformulation should use different keywords or phrasing to maximize
recall from a vector database.

Rules:
- Each query must be on its own line
- Do NOT number the queries
- Do NOT add explanations
- Each query should be a complete, standalone search query
- Vary vocabulary, specificity, and perspective across queries

Original query: {query}"""


# ══════════════════════════════════════════════════════════════════════════════
#  AnswerGeneration — query rewriting, multi-query, answer generation,
#                     citation verification, and streaming
# ══════════════════════════════════════════════════════════════════════════════


class AnswerGeneration:
    """Generate grounded, cited answers from retrieved documents using an LLM.

    Enhanced with:
        - **Multi-Query Retrieval** (Feature C): Generate diverse query variants
        - **Citation Verification** (Feature D): Post-generation citation checking
        - **Memory Summarization** (Feature F): Summarize older conversation turns
    """

    def __init__(self, config: Config):
        self.config = config

        # ── LLM ──────────────────────────────────────────────────────────
        self.llm = ChatGroq(
            model=self.config.LLM_MODEL_NAME,
            temperature=self.config.LLM_TEMPERATURE,
            api_key=self.config.GROQ_API_KEY,
            timeout=30,
        )

        # ── RAG chain (prompt → LLM → string) ───────────────────────────
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", _RAG_SYSTEM_PROMPT),
            ("user", "{question}"),
        ])
        # O1: name the chain so LangSmith traces read "rag_generate" instead
        # of an opaque RunnableSequence. No-op when tracing is disabled.
        self.chain = (self.prompt | self.llm | StrOutputParser()).with_config(
            {"run_name": "rag_generate"}
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_context_and_sources(retrieved_docs: List[Document]):
        """Turn a list of retrieved docs into a context string and a sources list.

        Returns:
            (context_str, sources_list) — ready for prompt injection and
            response metadata respectively.
        """
        context_parts = []
        sources = []

        for i, doc in enumerate(retrieved_docs, 1):
            meta = doc.metadata

            # Label each chunk so the LLM can cite it
            source_label = (
                f"[Source{i}: {meta.get('filename', 'unknown')}, "
                f"Page: {meta.get('page_number', 'N/A')}, "
                f"Type: {meta.get('chunk_type', 'text')}]"
            )
            context_parts.append(f"{source_label}\n{doc.page_content}\n")

            # BUG-8 fix: the context label above correctly defaults to
            # "N/A" via meta.get('page_number', 'N/A') — but .get() only
            # applies that default when the *key* is absent, not when it's
            # present with value None. This line used to plain
            # meta.get("page_number") with no default at all, so a missing
            # page became `None` here while the LLM (having seen "N/A" in
            # the label above) naturally cited "Page: N/A" — a mismatch
            # that made _verify_citations report a correct citation as
            # unverified.
            sources.append({
                "source_id": i,
                "filename": meta.get("filename"),
                "page": meta.get("page_number") or "N/A",
                "chunk_type": meta.get("chunk_type"),
                "chunk_id": meta.get("chunk_id"),
                "has_visual": bool(meta.get("has_visual")),  # B-hybrid: a page snapshot exists
            })

        context = "\n---\n".join(context_parts)
        return context, sources

    # ── Helper: Format history with Feature F ─────────────────────────────

    async def _format_history(self, chat_history: list) -> str:
        """Format chat history with optional async memory summarization (Feature F).

        Bug 2 fix: delegates to format_chat_history_async so the LLM summary
        call runs in a thread rather than blocking the event loop.
        """
        if not chat_history:
            return ""
        return await format_chat_history_async(
            chat_history,
            llm=self.llm if self.config.USE_MEMORY_SUMMARIZATION else None,
            use_summarization=self.config.USE_MEMORY_SUMMARIZATION,
        )

    # ── Feature C: Multi-Query Generation ─────────────────────────────────

    async def generate_multi_queries(self, query: str) -> List[str]:
        """Generate diverse reformulations of *query* for multi-query retrieval.

        Uses a single LLM call to produce ``config.MULTI_QUERY_COUNT`` variant
        queries that capture the same intent from different angles.

        Args:
            query: The original (possibly rewritten) search query.

        Returns:
            List of reformulated queries (always includes the original).
        """
        if not self.config.USE_MULTI_QUERY:
            return [query]

        try:
            multi_prompt = ChatPromptTemplate.from_messages([
                ("system", _MULTI_QUERY_PROMPT),
                ("user", "Generate the queries now."),
            ])
            chain = (multi_prompt | self.llm | StrOutputParser()).with_config(
                {"run_name": "multi_query_gen"}  # O1: readable LangSmith span
            )

            # BUG-3 fix: chain.invoke() blocks the event loop for the full
            # LLM round-trip; ainvoke() awaits it instead, letting other
            # requests run concurrently in the meantime.
            result = await chain.ainvoke({
                "query": query,
                "count": self.config.MULTI_QUERY_COUNT,
            })

            # Parse: one query per non-empty line
            variants = [
                line.strip()
                for line in result.strip().split("\n")
                if line.strip()
            ]

            # Always include the original query first
            all_queries = [query] + [v for v in variants if v.lower() != query.lower()]

            logger.info(
                "Multi-query: generated %d variants (from original + %d new)",
                len(all_queries), len(variants),
            )
            return all_queries

        except Exception as e:
            logger.warning("Multi-query generation failed, using original: %s", e)
            return [query]

    # ── Feature D: Citation Verification ──────────────────────────────────

    @staticmethod
    def _verify_citations(
        answer: str, sources: List[Dict]
    ) -> Dict[str, Any]:
        """Verify that citations in the answer name a real retrieved source.

        Parses patterns like ``[Source: filename, Page: X]`` and
        ``[SourceN: filename, Page: X]`` from the generated answer and checks
        each against the provided sources list.

        Q3: matching is **filename-level only** — the page number is kept in
        the returned citation string for display, but no longer gates
        verified/unverified. Page matching was noisy: chunking is per-page so
        a multi-page answer can correctly cite a page that isn't the chunk's
        single recorded page_number, and the B-hybrid multimodal path lets the
        LLM read whole page images, not just the chunk's tagged page. Filename
        membership is what actually answers "is this a real source?".

        Args:
            answer:  The LLM-generated answer text.
            sources: List of source metadata dicts from retrieval.

        Returns:
            Dict with ``verified``, ``unverified``, ``total``, and ``score``.
        """
        # Match patterns: [Source: file, Page: N] or [Source1: file, Page: N]
        citation_pattern = re.compile(
            r"\[Source\d*:\s*([^,\]]+),\s*Page:\s*([^\]]+)\]",
            re.IGNORECASE,
        )

        citations_found = citation_pattern.findall(answer)

        if not citations_found:
            return {
                "verified": [],
                "unverified": [],
                "total": 0,
                "score": 1.0,  # No citations to verify = perfect score
            }

        # Build a lookup set of real filenames (Q3: filename-level only).
        source_filenames = {str(s.get("filename", "")).strip().lower() for s in sources}

        verified = []
        unverified = []

        for cited_file, cited_page in citations_found:
            cited_file = cited_file.strip()
            cited_page = cited_page.strip()
            citation_str = f"[Source: {cited_file}, Page: {cited_page}]"

            if cited_file.lower() in source_filenames:
                verified.append(citation_str)
            else:
                unverified.append(citation_str)

        total = len(verified) + len(unverified)
        score = len(verified) / total if total > 0 else 1.0

        logger.info(
            "Citation verification: %d/%d verified (score=%.2f)",
            len(verified), total, score,
        )

        return {
            "verified": verified,
            "unverified": unverified,
            "total": total,
            "score": score,
        }

    # ── Query rewriting ───────────────────────────────────────────────────

    async def rewrite_query(self, query: str, chat_history: list = None) -> str:
        """Rewrite a follow-up question into a standalone search query.

        Uses conversation history to resolve pronouns and vague references
        so the vector-DB retrieval stays accurate on multi-turn chats.
        If there is no history, the original query is returned untouched.
        """
        if not chat_history:
            return query

        formatted_history = await self._format_history(chat_history)
        if not formatted_history:
            return query

        rewrite_prompt = ChatPromptTemplate.from_messages([
            ("system", _REWRITE_SYSTEM_PROMPT),
            ("user", "Follow-up question: {question}\nStandalone query:"),
        ])

        chain = (rewrite_prompt | self.llm | StrOutputParser()).with_config(
            {"run_name": "query_rewrite"}  # O1: readable LangSmith span
        )
        standalone_query = await chain.ainvoke({
            "chat_history": formatted_history,
            "question": query,
        })

        logger.info("Rewrote query: '%s' → '%s'", query, standalone_query.strip())
        return standalone_query.strip()

    # ── B-hybrid: multimodal message assembly ─────────────────────────────

    @staticmethod
    def _multimodal_messages(context: str, query: str, history_str: str, page_images: list):
        """Build a multimodal message list: system (rules + context), then the
        question plus the rendered page image(s) for the model to read in place."""
        from langchain_core.messages import HumanMessage, SystemMessage

        system = _RAG_SYSTEM_PROMPT.format(chat_history=history_str, context=context)
        content = [{"type": "text", "text": query}]
        for b64 in page_images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        return [SystemMessage(content=system), HumanMessage(content=content)]

    # ── Answer generation (blocking) ──────────────────────────────────────

    async def generate(
        self,
        query: str,
        retrieved_docs: List[Document],
        chat_history: list = None,
        page_images: list = None,
    ) -> Dict:
        """Generate a complete answer from retrieved docs (waits for full response).

        Returns:
            Dict with keys ``answer``, ``sources``, ``num_sources_used``,
            and optionally ``citation_verification`` (Feature D).
        """
        context, sources = self._build_context_and_sources(retrieved_docs)
        history_str = await self._format_history(chat_history) if chat_history else ""

        # B-hybrid: if a retrieved chunk sits on a visual page, answer over the
        # rendered page image(s) too — only when a vision model is configured.
        # Off by default now (USE_IMAGE_ANSWERING=False) since Groq Llama-3.1-8B
        # is text-only, so page_images is empty and this stays on the text chain.
        # BUG-3 fix: ainvoke() awaits the LLM instead of blocking the event loop.
        if page_images:
            messages = self._multimodal_messages(context, query, history_str, page_images)
            answer = (await self.llm.ainvoke(messages)).content
        else:
            answer = await self.chain.ainvoke({
                "context": context,
                "question": query,
                "chat_history": history_str,
            })

        result = {
            "answer": answer,
            "sources": sources,
            "num_sources_used": len(retrieved_docs),
        }

        # Feature D: Citation verification (post-generation, no latency on response)
        if self.config.USE_CITATION_VERIFICATION:
            result["citation_verification"] = self._verify_citations(answer, sources)

        return result

    # ── Answer generation (streaming via SSE) ─────────────────────────────

    async def generate_stream(
        self,
        query: str,
        retrieved_docs: List[Document],
        chat_history: list = None,
        capture: dict = None,
        page_images: list = None,
    ):
        """Yield Server-Sent Events (SSE) for real-time streaming responses.

        Async generator — use with FastAPI's StreamingResponse.

        Event sequence:
            1. ``type: sources``               — source metadata (sent once, up front)
            2. ``type: token``                 — individual LLM tokens (many events)
            3. ``type: citation_verification`` — (Feature D, if enabled)
            4. ``[DONE]``                      — stream terminator (always sent)
        """
        context, sources = self._build_context_and_sources(retrieved_docs)

        # Add a text snippet to each source (useful for the frontend)
        for i, doc in enumerate(retrieved_docs):
            sources[i]["content"] = doc.page_content[:250].replace("\n", " ") + "..."

        # 1. Send sources up front
        yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

        # 2. Stream LLM tokens
        full_answer = ""
        chat_history_str = await self._format_history(chat_history) if chat_history else ""

        # O4: when tracing is on, collect this generation's LangSmith run so we
        # can hand its run_id to the client — a later 👍/👎 attaches feedback to
        # this exact trace. The collector shares the run_id the LangChainTracer
        # sends to LangSmith. Tracing off => no collector, no run_id, no feedback.
        run_id = None
        run_cm = collect_runs() if self.config.LANGSMITH_TRACING else nullcontext()

        # BUG-3 fix: self.chain.stream(...) is a *sync* iterator — pulling
        # from it with a plain `for` blocks the event loop on every single
        # token, serializing every concurrent request and defeating the
        # point of SSE streaming. astream() + `async for` yields control
        # back between tokens instead.
        try:
            with run_cm as run_collector:
                if page_images:
                    # B-hybrid multimodal stream: llm.astream yields AIMessageChunks
                    # whose .content is the text delta (read it directly).
                    messages = self._multimodal_messages(context, query, chat_history_str, page_images)
                    async for chunk in self.llm.astream(messages):
                        text = chunk.content if isinstance(chunk.content, str) else ""
                        if text:
                            full_answer += text
                            yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
                else:
                    async for chunk in self.chain.astream({
                        "context": context,
                        "question": query,
                        "chat_history": chat_history_str,
                    }):
                        # StrOutputParser's astream() always yields a str-like chunk.
                        if chunk:
                            full_answer += chunk
                            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                # The root run is finalized once the stream is fully consumed.
                if run_collector is not None and getattr(run_collector, "traced_runs", None):
                    run_id = str(run_collector.traced_runs[0].id)
        except Exception as e:
            logger.error("SSE stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Stream interrupted. Please try again.'})}\n\n"

        # 3. Citation verification (Feature D) — runs AFTER streaming, before [DONE]
        verification = None
        if self.config.USE_CITATION_VERIFICATION and full_answer:
            verification = self._verify_citations(full_answer, sources)
            yield f"data: {json.dumps({'type': 'citation_verification', **verification})}\n\n"

        # C1: hand the finished answer back to the caller (pipeline.query_stream)
        # via a side channel so it can be cached without re-parsing this SSE.
        if capture is not None and full_answer:
            capture["answer"] = full_answer
            capture["sources"] = sources
            if verification is not None:
                capture["citation_verification"] = verification

        # O4: surface the trace id so the client can attach 👍/👎 feedback to this
        # exact answer. Only present when tracing is on (else nothing to score).
        if run_id:
            yield f"data: {json.dumps({'type': 'meta', 'run_id': run_id})}\n\n"

        # 4. Always signal end-of-stream so the client never hangs
        yield "data: [DONE]\n\n"



# ══════════════════════════════════════════════════════════════════════════════
#  Quick test — retrieve & generate an answer about the Smart Signal PDF
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    config = Config()
    retrieval_manager = RetrievalManager(config=config)
    generator = AnswerGeneration(config=config)

    query = "How does Smart Signal use reinforcement learning for traffic control?"
    docs = retrieval_manager.retrieve(query)
    results = generator.generate(query, docs)
    print(results["answer"])