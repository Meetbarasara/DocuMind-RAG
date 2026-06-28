"""Persistent chat history API: /api/conversations.

Round-trips create → list and append → load, and checks the key isolation
property: every route scopes by the JWT's user_id, so a conversation id that
belongs to another user yields nothing (no cross-user read). Supabase is mocked.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db
from src.api.main import app


class FakeDb:
    """In-memory stand-in for SupabaseManager's chat-history methods."""

    def __init__(self):
        self.convos: list = []
        self.messages: list = []
        self._n = 0

    def create_conversation(self, user_id, title="New chat"):
        self._n += 1
        row = {"id": f"c{self._n}", "user_id": user_id, "title": title, "updated_at": "t0"}
        self.convos.append(row)
        return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"]}

    def list_conversations(self, user_id):
        return [
            {"id": c["id"], "title": c["title"], "updated_at": c["updated_at"]}
            for c in self.convos if c["user_id"] == user_id
        ]

    def get_conversation_messages(self, user_id, conversation_id):
        return [
            {k: m[k] for k in ("role", "content", "sources", "run_id")}
            for m in self.messages
            if m["conversation_id"] == conversation_id and m["user_id"] == user_id
        ]

    def add_message(self, user_id, conversation_id, role, content, sources=None, run_id=None):
        m = {"conversation_id": conversation_id, "user_id": user_id, "role": role,
             "content": content, "sources": sources, "run_id": run_id}
        self.messages.append(m)
        return m

    def delete_conversation(self, user_id, conversation_id):
        before = len(self.convos)
        self.convos = [c for c in self.convos
                       if not (c["id"] == conversation_id and c["user_id"] == user_id)]
        return len(self.convos) < before


# A mutable holder so a test can switch which user is "logged in".
_current = {"id": "user-A"}


async def _fake_current_user():
    return {"user": SimpleNamespace(id=_current["id"]), "access_token": "t"}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _setup():
    _current["id"] = "user-A"
    fake_db = FakeDb()
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_then_list(client):
    resp = await client.post("/api/conversations", json={"title": "Smart Signal Q&A"})
    assert resp.status_code == 201
    conv_id = resp.json()["id"]

    listed = await client.get("/api/conversations")
    assert listed.status_code == 200
    convos = listed.json()["conversations"]
    assert [c["id"] for c in convos] == [conv_id]
    assert convos[0]["title"] == "Smart Signal Q&A"


@pytest.mark.asyncio
async def test_append_and_load_messages(client):
    conv_id = (await client.post("/api/conversations", json={})).json()["id"]

    await client.post(f"/api/conversations/{conv_id}/messages",
                      json={"role": "human", "content": "what is smart signal"})
    await client.post(f"/api/conversations/{conv_id}/messages",
                      json={"role": "ai", "content": "It is...", "sources": [{"filename": "x.pdf"}], "run_id": "r1"})

    msgs = (await client.get(f"/api/conversations/{conv_id}/messages")).json()["messages"]
    assert [m["role"] for m in msgs] == ["human", "ai"]
    assert msgs[1]["content"] == "It is..."
    assert msgs[1]["sources"] == [{"filename": "x.pdf"}]
    assert msgs[1]["run_id"] == "r1"


@pytest.mark.asyncio
async def test_messages_are_scoped_to_the_owner(client):
    # user-A creates a conversation with a message
    conv_id = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(f"/api/conversations/{conv_id}/messages",
                      json={"role": "human", "content": "secret"})

    # user-B asks for A's conversation by id -> sees nothing
    _current["id"] = "user-B"
    msgs = (await client.get(f"/api/conversations/{conv_id}/messages")).json()["messages"]
    assert msgs == []

    # ...and B's own conversation list is empty
    assert (await client.get("/api/conversations")).json()["conversations"] == []


@pytest.mark.asyncio
async def test_delete_conversation(client):
    conv_id = (await client.post("/api/conversations", json={})).json()["id"]

    resp = await client.delete(f"/api/conversations/{conv_id}")
    assert resp.status_code == 200
    assert (await client.get("/api/conversations")).json()["conversations"] == []
