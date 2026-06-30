"""Regression test for BUG-3 (see BUGFIXES.md).

generation.py used synchronous chain.invoke()/chain.stream() inside async
methods (generate, generate_stream, generate_multi_queries). A sync call
blocks FastAPI's single-threaded event loop for its entire duration, so two
concurrent requests serialize instead of overlapping — defeating the whole
point of async/SSE for a multi-user server.

Each test runs two calls concurrently via asyncio.gather and measures wall
time: a blocking implementation forces them to serialize (~2x one call's
delay); a truly async one lets them overlap (~1x one call's delay).
"""

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage

from src.components.config import Config
from src.components.generation import AnswerGeneration

_DELAY = 0.2
# Comfortably between "ran concurrently" (~1x _DELAY) and "serialized" (~2x _DELAY).
_CONCURRENT_THRESHOLD = _DELAY * 1.5


class FakeChain:
    """Stands in for `self.prompt | self.llm | StrOutputParser()`."""

    def __init__(self, delay=_DELAY, chunks=3):
        self.delay = delay
        self.chunks = chunks

    def invoke(self, inputs):
        time.sleep(self.delay)  # blocking, like the real sync HTTP call inside LangChain
        return "sync answer"

    async def ainvoke(self, inputs):
        await asyncio.sleep(self.delay)  # yields to the event loop
        return "async answer"

    def stream(self, inputs):
        per_chunk = self.delay / self.chunks
        for i in range(self.chunks):
            time.sleep(per_chunk)
            yield f"chunk{i} "

    async def astream(self, inputs):
        per_chunk = self.delay / self.chunks
        for i in range(self.chunks):
            await asyncio.sleep(per_chunk)
            yield f"chunk{i} "


@pytest.fixture
def generator():
    return AnswerGeneration(Config())


@pytest.mark.asyncio
async def test_generate_does_not_block_event_loop(generator):
    generator.chain = FakeChain()

    start = time.perf_counter()
    await asyncio.gather(
        generator.generate("q1", []),
        generator.generate("q2", []),
    )
    elapsed = time.perf_counter() - start

    assert elapsed < _CONCURRENT_THRESHOLD, (
        f"generate() blocked the event loop: 2 concurrent calls took {elapsed:.2f}s, "
        f"expected ~{_DELAY:.2f}s if truly concurrent"
    )


@pytest.mark.asyncio
async def test_generate_stream_does_not_block_event_loop(generator):
    generator.chain = FakeChain()

    async def consume(agen):
        return [chunk async for chunk in agen]

    start = time.perf_counter()
    await asyncio.gather(
        consume(generator.generate_stream("q1", [])),
        consume(generator.generate_stream("q2", [])),
    )
    elapsed = time.perf_counter() - start

    assert elapsed < _CONCURRENT_THRESHOLD, (
        f"generate_stream() blocked the event loop: 2 concurrent calls took {elapsed:.2f}s, "
        f"expected ~{_DELAY:.2f}s if truly concurrent"
    )


@pytest.mark.asyncio
async def test_generate_multi_queries_does_not_block_event_loop(generator, monkeypatch):
    """Also implicitly proves generate_multi_queries became awaitable.

    Pre-fix it's a plain `def`, so calling it just runs synchronously and
    returns a list — asyncio.gather(list, list) raises TypeError before the
    timing assertion is even reached. That's still a legitimate failure: the
    function isn't awaitable yet, which is exactly the bug.

    Patches ChatGroq.invoke/.ainvoke on the *class* (via monkeypatch,
    so it's reverted after the test) rather than the instance — the chat model is a
    Pydantic model and rejects `instance.invoke = ...` with a ValueError
    ("no field 'invoke'") since that's not a declared field.
    """

    def fake_invoke(self, prompt_value, *args, **kwargs):
        time.sleep(_DELAY)
        return AIMessage(content="variant one\nvariant two")

    async def fake_ainvoke(self, prompt_value, *args, **kwargs):
        await asyncio.sleep(_DELAY)
        return AIMessage(content="variant one\nvariant two")

    monkeypatch.setattr(type(generator.llm), "invoke", fake_invoke)
    monkeypatch.setattr(type(generator.llm), "ainvoke", fake_ainvoke)

    # L3 flipped USE_MULTI_QUERY off by default; force it on so this test still
    # exercises the LLM-backed multi-query path it exists to cover.
    generator.config.USE_MULTI_QUERY = True

    start = time.perf_counter()
    await asyncio.gather(
        generator.generate_multi_queries("q1"),
        generator.generate_multi_queries("q2"),
    )
    elapsed = time.perf_counter() - start

    assert elapsed < _CONCURRENT_THRESHOLD, (
        f"generate_multi_queries() blocked the event loop: 2 concurrent calls took "
        f"{elapsed:.2f}s, expected ~{_DELAY:.2f}s if truly concurrent"
    )
