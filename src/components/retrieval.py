import os

from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore

from src.components.config import Config
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  RetrievalManager — similarity search against Pinecone with score filtering
# ══════════════════════════════════════════════════════════════════════════════


class RetrievalManager:
    """Retrieve relevant documents from a Pinecone vector store."""

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

    # ── Retrieval ─────────────────────────────────────────────────────────

    def retrieve(self, query: str, filename_filter: str = None, page_filter: str = None):
        """Search Pinecone for documents similar to *query*.

        Args:
            query:           The user's question or search string.
            filename_filter: If set, only return chunks from this filename.
            page_filter:     If set (along with *filename_filter*), narrow to a page.

        Returns:
            List of ``Document`` objects whose cosine similarity score is
            at or above ``Config.SIMILARITY_THRESHOLD``.
        """
        similarity_threshold = self.config.SIMILARITY_THRESHOLD

        try:
            # Build optional metadata filter
            filter_dict = {}
            if filename_filter:
                filter_dict["filename"] = filename_filter
                if page_filter:
                    filter_dict["page_number"] = page_filter

            # Run similarity search (with scores for filtering)
            docs_and_scores = self.vectorstore.similarity_search_with_score(
                query,
                k=self.config.TOP_K,
                filter=filter_dict or None,
            )

            # Keep only results above the quality threshold
            docs = [doc for doc, score in docs_and_scores if score >= similarity_threshold]

            logger.info(
                "Retrieved %d/%d docs above threshold (%.2f)",
                len(docs), len(docs_and_scores), similarity_threshold,
            )
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