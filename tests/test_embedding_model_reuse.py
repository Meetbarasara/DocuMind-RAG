"""Regression test for Latency Optimization #5 (see BUGFIXES.md / CODE_REVIEW.md §4).

EmbeddingManager.create_vector_store built a brand new HuggingFaceEmbeddings(...)
on every call -- i.e. once per file upload -- instead of constructing it
once and reusing it. Each construction sets up a fresh underlying HTTP
client with no connection-pool reuse across calls, pure overhead repeated
on every upload for no behavioral benefit (same model name, same API key,
every time).
"""

from langchain_core.documents import Document

import src.components.embeddings as embeddings_module
from src.components.config import Config
from src.components.embeddings import EmbeddingManager


class _CountingEmbeddings:
    """Stands in for HuggingFaceEmbeddings -- counts how many times it's constructed."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1


class _FakeVectorStore:
    """Stands in for PineconeVectorStore -- no real network calls."""

    def __init__(self, *args, **kwargs):
        pass

    def add_documents(self, documents, ids):
        pass


def test_embedding_model_constructed_once_and_reused_across_calls(monkeypatch):
    _CountingEmbeddings.instances = 0
    monkeypatch.setattr(embeddings_module, "HuggingFaceEmbeddings", _CountingEmbeddings)
    monkeypatch.setattr(embeddings_module, "PineconeVectorStore", _FakeVectorStore)

    manager = EmbeddingManager(Config(PINECONE_API_KEY="fake", GROQ_API_KEY="gsk-fake"))

    doc1 = Document(page_content="hello world", metadata={"filename": "a.txt"})
    doc2 = Document(page_content="goodbye world", metadata={"filename": "b.txt"})

    manager.create_vector_store([doc1], namespace="ns1")
    manager.create_vector_store([doc2], namespace="ns2")

    assert _CountingEmbeddings.instances == 1, (
        "expected the embedding model to be constructed once (in __init__) and "
        f"reused across calls, but got {_CountingEmbeddings.instances} separate "
        "instances across 2 create_vector_store calls"
    )


def test_embedding_model_reused_even_on_the_no_documents_early_exit(monkeypatch):
    """The "nothing to embed" branch built its own local embedding_model too --
    make sure that path also reuses the same instance, not a third one."""
    _CountingEmbeddings.instances = 0
    monkeypatch.setattr(embeddings_module, "HuggingFaceEmbeddings", _CountingEmbeddings)
    monkeypatch.setattr(embeddings_module, "PineconeVectorStore", _FakeVectorStore)

    manager = EmbeddingManager(Config(PINECONE_API_KEY="fake", GROQ_API_KEY="gsk-fake"))

    manager.create_vector_store([], namespace="ns-empty")
    doc = Document(page_content="hello world", metadata={"filename": "a.txt"})
    manager.create_vector_store([doc], namespace="ns1")

    assert _CountingEmbeddings.instances == 1
