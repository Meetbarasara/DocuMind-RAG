import json
import logging
from typing import Dict, List

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from src.components.config import Config
from src.components.retrieval import RetrievalManager
from src.logger import get_logger
from src.utils import format_chat_history

logger = get_logger(__name__)

# Silence noisy httpx logs (emitted on every OpenAI API call)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
#  Prompt templates
# ══════════════════════════════════════════════════════════════════════════════

_RAG_SYSTEM_PROMPT = """\
You are a helpful assistant answering questions based on provided documents.

Rules:
1. Only use information from the context below
2. If answer is not in context, say "I cannot find information about your question from the document"
3. Cite sources using [Source: filename, Page: X] format
4. Be concise but complete
5. Use conversation history only to understand follow-up questions, not as a source of facts

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


# ══════════════════════════════════════════════════════════════════════════════
#  AnswerGeneration — query rewriting, answer generation, and streaming
# ══════════════════════════════════════════════════════════════════════════════


class AnswerGeneration:
    """Generate grounded, cited answers from retrieved documents using an LLM."""

    def __init__(self, config: Config):
        self.config = config

        # ── LLM ──────────────────────────────────────────────────────────
        self.llm = ChatOpenAI(
            model=self.config.LLM_MODEL_NAME,
            temperature=self.config.LLM_TEMPERATURE,
            api_key=self.config.OPENAI_API_KEY,
            request_timeout=30,
        )

        # ── RAG chain (prompt → LLM → string) ───────────────────────────
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", _RAG_SYSTEM_PROMPT),
            ("user", "{question}"),
        ])
        self.chain = self.prompt | self.llm | StrOutputParser()

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

            sources.append({
                "source_id": i,
                "filename": meta.get("filename"),
                "page": meta.get("page_number"),
                "chunk_type": meta.get("chunk_type"),
                "chunk_id": meta.get("chunk_id"),
            })

        context = "\n---\n".join(context_parts)
        return context, sources

    # ── Query rewriting ───────────────────────────────────────────────────

    def rewrite_query(self, query: str, chat_history: list = None) -> str:
        """Rewrite a follow-up question into a standalone search query.

        Uses conversation history to resolve pronouns and vague references
        so the vector-DB retrieval stays accurate on multi-turn chats.
        If there is no history, the original query is returned untouched.
        """
        if not chat_history:
            return query

        formatted_history = format_chat_history(chat_history)
        if not formatted_history:
            return query

        rewrite_prompt = ChatPromptTemplate.from_messages([
            ("system", _REWRITE_SYSTEM_PROMPT),
            ("user", "Follow-up question: {question}\nStandalone query:"),
        ])

        chain = rewrite_prompt | self.llm | StrOutputParser()
        standalone_query = chain.invoke({
            "chat_history": formatted_history,
            "question": query,
        })

        logger.info("Rewrote query: '%s' → '%s'", query, standalone_query.strip())
        return standalone_query.strip()

    # ── Answer generation (blocking) ──────────────────────────────────────

    def generate(
        self,
        query: str,
        retrieved_docs: List[Document],
        chat_history: list = None,
    ) -> Dict:
        """Generate a complete answer from retrieved docs (waits for full response).

        Returns:
            Dict with keys ``answer``, ``sources``, and ``num_sources_used``.
        """
        context, sources = self._build_context_and_sources(retrieved_docs)

        answer = self.chain.invoke({
            "context": context,
            "question": query,
            "chat_history": format_chat_history(chat_history) if chat_history else "",
        })

        return {
            "answer": answer,
            "sources": sources,
            "num_sources_used": len(retrieved_docs),
        }

    # ── Answer generation (streaming via SSE) ─────────────────────────────

    def generate_stream(
        self,
        query: str,
        retrieved_docs: List[Document],
        chat_history: list = None,
    ):
        """Yield Server-Sent Events (SSE) for real-time streaming responses.

        Event sequence:
            1. ``type: sources``  — source metadata (sent once, up front)
            2. ``type: token``    — individual LLM tokens (many events)
            3. ``[DONE]``         — stream terminator (always sent)
        """
        context, sources = self._build_context_and_sources(retrieved_docs)

        # Add a text snippet to each source (useful for the frontend)
        for i, doc in enumerate(retrieved_docs):
            sources[i]["content"] = doc.page_content[:250].replace("\n", " ") + "..."

        # 1. Send sources up front
        yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

        # 2. Stream LLM tokens
        stream = self.chain.stream({
            "context": context,
            "question": query,
            "chat_history": format_chat_history(chat_history) if chat_history else "",
        })

        try:
            for chunk in stream:
                text_chunk = chunk if isinstance(chunk, str) else chunk.get("answer", "")
                if text_chunk:
                    yield f"data: {json.dumps({'type': 'token', 'content': text_chunk})}\n\n"
        except Exception as e:
            logger.error("SSE stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Stream interrupted. Please try again.'})}\n\n"
        finally:
            # 3. Always signal end-of-stream so the client never hangs
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