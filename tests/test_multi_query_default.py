"""L3: multi-query retrieval is OFF by default.

Multi-query adds an LLM round-trip (to generate variants) plus N extra
retrievals/embeddings on the hot path. Once Cohere reranking handles precision,
that cost rarely earns its keep, so the default is now off — but the feature
stays available as an opt-in high-recall flag. These pin the default and prove
that, when disabled, generate_multi_queries short-circuits with no LLM call.
"""

import pytest

from src.components.config import Config
from src.components.generation import AnswerGeneration


def test_multi_query_off_by_default():
    assert Config().USE_MULTI_QUERY is False


@pytest.mark.asyncio
async def test_generate_multi_queries_skips_llm_when_disabled(monkeypatch):
    gen = AnswerGeneration(Config(USE_MULTI_QUERY=False))

    called = {"llm": False}

    async def boom(self, *a, **k):
        called["llm"] = True
        raise AssertionError("LLM must not be called when multi-query is disabled")

    monkeypatch.setattr(type(gen.llm), "ainvoke", boom)

    out = await gen.generate_multi_queries("what is X?")

    assert out == ["what is X?"]   # just the original query, no variants
    assert called["llm"] is False  # no LLM round-trip on the hot path
