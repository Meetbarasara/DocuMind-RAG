"""Regression test for BUG-15 (see BUGFIXES.md).

evaluate.py's routes were async def but called EvaluationManager's sync,
blocking evaluate_single/evaluate_batch directly — real RAGAS evaluation
makes multiple LLM calls per item, so this blocked the event loop for the
whole evaluation. The routes also had no rate limit despite being one of
the most expensive operations exposed by the app (any authenticated user
could run batch evals unbounded).
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_eval_manager
from src.api.main import app

_DELAY = 0.2
_CONCURRENT_THRESHOLD = _DELAY * 1.5


class FakeEvalManager:
    def evaluate_single(self, query, answer, contexts, ground_truth=None):
        time.sleep(_DELAY)  # simulates RAGAS's real blocking LLM round-trips
        return {"faithfulness": 1.0}

    def evaluate_batch(self, test_set):
        time.sleep(_DELAY)
        return {"scores": [], "summary": {}}


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


@pytest.fixture(autouse=True)
def override_deps():
    app.dependency_overrides[get_eval_manager] = lambda: FakeEvalManager()
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_evaluate_single_does_not_block_event_loop(client):
    payload = {"question": "q", "answer": "a", "contexts": ["c"]}

    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/evaluate/single", json=payload),
        client.post("/api/evaluate/single", json=payload),
    )
    elapsed = time.perf_counter() - start

    assert elapsed < _CONCURRENT_THRESHOLD, (
        f"evaluate_single blocked the event loop: 2 concurrent calls took {elapsed:.2f}s, "
        f"expected ~{_DELAY:.2f}s if truly concurrent"
    )


@pytest.mark.asyncio
async def test_evaluate_batch_does_not_block_event_loop(client):
    payload = {"test_set": [{"question": "q", "answer": "a", "contexts": ["c"]}]}

    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/evaluate/batch", json=payload),
        client.post("/api/evaluate/batch", json=payload),
    )
    elapsed = time.perf_counter() - start

    assert elapsed < _CONCURRENT_THRESHOLD, (
        f"evaluate_batch blocked the event loop: 2 concurrent calls took {elapsed:.2f}s, "
        f"expected ~{_DELAY:.2f}s if truly concurrent"
    )


@pytest.mark.asyncio
async def test_evaluate_single_is_rate_limited(client):
    payload = {"question": "q", "answer": "a", "contexts": ["c"]}

    statuses = []
    for _ in range(10):
        resp = await client.post("/api/evaluate/single", json=payload)
        statuses.append(resp.status_code)

    assert statuses[0] != 429, "the very first request should never be throttled"
    assert 429 in statuses, f"expected a 429 somewhere in 10 rapid eval calls, got {statuses}"
