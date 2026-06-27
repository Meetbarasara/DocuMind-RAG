"""O4: 👍/👎 feedback loop.

Two halves:
  1. generate_stream must surface the LangSmith run_id as a `meta` SSE event
     (only when tracing is on) so the UI can attach feedback to the exact trace.
  2. POST /api/chat/feedback records that run_id as a LangSmith score — and is a
     no-op when tracing is off (there's no trace to attach to).

Everything is mocked; nothing hits LangSmith or the network.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_pipeline
from src.api.main import app
from src.components.generation import AnswerGeneration


# ── 1. generate_stream: meta run_id event ───────────────────────────────────

class _FakeChain:
    async def astream(self, _inputs):
        for t in ["Hello ", "world"]:
            yield t


def _make_gen(tracing: bool) -> AnswerGeneration:
    gen = AnswerGeneration.__new__(AnswerGeneration)
    gen.config = SimpleNamespace(USE_CITATION_VERIFICATION=False, LANGSMITH_TRACING=tracing)
    gen.chain = _FakeChain()
    return gen


async def _drain(agen):
    return "".join([e async for e in agen])


@pytest.mark.asyncio
async def test_stream_emits_run_id_meta_when_tracing_on(monkeypatch):
    @contextmanager
    def fake_collect_runs():
        yield SimpleNamespace(traced_runs=[SimpleNamespace(id="run-123")])

    monkeypatch.setattr("src.components.generation.collect_runs", fake_collect_runs)

    body = await _drain(_make_gen(tracing=True).generate_stream("q", retrieved_docs=[]))

    assert '"type": "meta"' in body
    assert "run-123" in body
    # meta must arrive before the stream terminator
    assert body.index("run-123") < body.index("[DONE]")


@pytest.mark.asyncio
async def test_stream_has_no_meta_when_tracing_off():
    body = await _drain(_make_gen(tracing=False).generate_stream("q", retrieved_docs=[]))

    assert '"type": "meta"' not in body
    assert "[DONE]" in body


# ── 2. POST /api/chat/feedback ──────────────────────────────────────────────

_created: list = []


class _FakeLangsmithClient:
    def create_feedback(self, run_id, key, score=None, comment=None):
        _created.append({"run_id": run_id, "key": key, "score": score, "comment": comment})


class _FakePipeline:
    def __init__(self, tracing: bool):
        self.config = SimpleNamespace(LANGSMITH_TRACING=tracing)


async def _fake_user():
    return {"user": SimpleNamespace(id="u1"), "access_token": "t"}


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _created.clear()
    monkeypatch.setattr("langsmith.Client", _FakeLangsmithClient)
    app.dependency_overrides[get_current_user] = _fake_user
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_feedback_records_langsmith_score(client):
    app.dependency_overrides[get_pipeline] = lambda: _FakePipeline(tracing=True)

    resp = await client.post("/api/chat/feedback", json={"run_id": "run-123", "score": 1.0})

    assert resp.status_code == 204
    assert _created == [{"run_id": "run-123", "key": "user_score", "score": 1.0, "comment": None}]


@pytest.mark.asyncio
async def test_feedback_noop_when_tracing_off(client):
    app.dependency_overrides[get_pipeline] = lambda: _FakePipeline(tracing=False)

    resp = await client.post("/api/chat/feedback", json={"run_id": "run-123", "score": 0.0})

    assert resp.status_code == 204
    assert _created == []   # tracing off -> nothing sent to LangSmith
