"""Regression test for the PINECONE_NAMESPACE LOW nit (see BUGFIXES.md).

Config.PINECONE_NAMESPACE defaults to "" ("must be set per-user at
runtime" per its own comment). Pinecone treats namespace="" as a real,
literal namespace — the default one, shared by anyone else who also
forgets to pass one. A caller that forgets to pass a real namespace into
RAGPipeline._get_retrieval_manager (the one real chokepoint every query/
ingest/delete goes through) would silently read/write that shared bucket
instead of failing loudly.
"""

from types import SimpleNamespace

import pytest

from src.components.config import Config
from src.exception import CustomException
from src.pipeline.pipeline import RAGPipeline


@pytest.fixture
def pipeline():
    return RAGPipeline(Config())


def test_empty_namespace_is_rejected(pipeline):
    with pytest.raises(CustomException):
        pipeline._get_retrieval_manager("")


def test_real_namespace_still_works(pipeline, monkeypatch):
    # Real RetrievalManager.__init__ hits Pinecone's network immediately
    # (confirmed in BUG-4/5's investigation) — fake it out so this test
    # only proves the guard doesn't reject a legitimate namespace, without
    # depending on network access.
    monkeypatch.setattr(
        "src.pipeline.pipeline.RetrievalManager",
        lambda cfg: SimpleNamespace(config=cfg),
    )

    rm = pipeline._get_retrieval_manager("real-user-id")

    assert rm.config.PINECONE_NAMESPACE == "real-user-id"
