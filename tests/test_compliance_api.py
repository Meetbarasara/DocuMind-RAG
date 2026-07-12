"""Compliance gap-analysis API: /api/compliance.

Drives the SSE check end-to-end with a fake judge LLM + fake retrieval (so the
REAL engine — run_check, judge_requirement, quote→chunk citation matching,
summarize — is exercised, only the network is faked), and checks:
  - streamed rows + final summary + persistence,
  - the float→int page-number normalisation (Pinecone returns page 2 as 2.0),
  - 404 / 409 / 503 validation paths,
  - per-user scoping of persisted checks.
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langchain_core.documents import Document

import src.api.router.compliance as compliance_router
from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app
from src.components.judge import JudgeNotConfigured


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeJudge:
    """Stand-in judge LLM: verdict depends on the requirement text so we get one
    cited Partial and one evidence-less Gap."""

    async def ainvoke(self, messages):
        human = messages[-1].content.lower()
        if "beneficial owner" in human:
            content = '{"status": "Gap", "policy_quote": "", "confidence": 0.9, "rationale": "not addressed"}'
        else:
            content = ('{"status": "Partial",'
                       ' "policy_quote": "We retain records for three years after closure.",'
                       ' "confidence": 0.8, "rationale": "only three years, less than five"}')
        return SimpleNamespace(content=content)


class FakeRM:
    """Retrieval manager stub: returns one policy chunk whose page_number is a
    float (2.0), mimicking Pinecone's numeric metadata, and whose text contains
    the quote FakeJudge cites (so citation matching resolves it)."""

    def retrieve(self, query, *args, **kwargs):
        return [Document(
            page_content="We retain records for three years after closure.",
            metadata={"filename": "acme_policy.pdf", "page_number": 2.0},
        )]


class FakePipeline:
    def __init__(self):
        import tempfile

        # build_judge_llm is monkeypatched → unused; the upload fields feed the
        # regulation-upload endpoint's validation + temp write.
        self.config = SimpleNamespace(
            SUPPORTED_FILE_TYPES=("pdf", "docx", "txt"),
            MAX_UPLOAD_SIZE_BYTES=50 * 1024 * 1024,
            UPLOAD_DIR=tempfile.gettempdir(),
            USE_CLAUSE_AWARE_CHUNKING=True,
        )

    def _get_retrieval_manager(self, namespace):
        return FakeRM()

    def ingest_file(self, path, namespace="", clause_aware=False):
        return 3


class FakeDb:
    def __init__(self):
        self.regs = {
            "reg-1": {
                "id": "reg-1", "name": "RBI KYC Master Direction", "regulator": "RBI",
                "requirements": [
                    {"id": "req-1", "text": "Retain KYC records for at least five years.",
                     "page": 3, "section": None},
                    {"id": "req-2", "text": "Identify the beneficial owners of legal-entity customers.",
                     "page": 2, "section": None},
                ],
            },
            "reg-empty": {"id": "reg-empty", "name": "Empty Reg", "regulator": "RBI",
                          "requirements": []},
        }
        self.checks: list = []
        self._n = 0

    def list_regulations(self):
        return [{"id": r["id"], "name": r["name"], "regulator": r["regulator"]}
                for r in self.regs.values()]

    def get_regulation(self, regulation_id):
        return self.regs.get(regulation_id)

    def upsert_regulation(self, name, regulator=None, circular_id=None, requirements=None,
                          namespace="regulations"):
        rid = f"reg-{name}"
        self.regs[rid] = {"id": rid, "name": name, "regulator": regulator,
                          "requirements": requirements or []}
        return {"id": rid, "name": name}

    def save_compliance_check(self, user_id, policy_label, regulation_id, summary, rows):
        self._n += 1
        row = {"id": f"chk{self._n}", "user_id": user_id, "policy_label": policy_label,
               "regulation_id": regulation_id, "summary": summary, "rows": rows,
               "created_at": "t0"}
        self.checks.append(row)
        return row

    def list_compliance_checks(self, user_id):
        return [{k: c[k] for k in ("id", "policy_label", "regulation_id", "summary", "created_at")}
                for c in self.checks if c["user_id"] == user_id]

    def get_compliance_check(self, user_id, check_id):
        for c in self.checks:
            if c["id"] == check_id and c["user_id"] == user_id:
                return c
        return None


# ── Wiring ──────────────────────────────────────────────────────────────────

_current = {"id": "user-A"}


async def _fake_current_user():
    return {"user": SimpleNamespace(id=_current["id"]), "access_token": "t"}


def _events(text: str):
    """Parse an SSE body into a list of event dicts ([DONE] → {'type': 'DONE'})."""
    out = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        body = line[len("data: "):]
        out.append({"type": "DONE"} if body.strip() == "[DONE]" else json.loads(body))
    return out


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _current["id"] = "user-A"
    # One shared instance each — a persisted check must survive across the
    # separate POST-then-GET requests within a test.
    fake_db = FakeDb()
    fake_pipeline = FakePipeline()
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_pipeline] = lambda: fake_pipeline
    app.dependency_overrides[get_current_user] = _fake_current_user
    # Default: a working fake judge. The 503 test re-patches this to raise.
    monkeypatch.setattr(compliance_router, "build_judge_llm", lambda config, **kw: FakeJudge())
    yield
    app.dependency_overrides.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_regulations(client):
    resp = await client.get("/api/compliance/regulations")
    assert resp.status_code == 200
    assert "RBI KYC Master Direction" in [r["name"] for r in resp.json()["regulations"]]


@pytest.mark.asyncio
async def test_check_streams_rows_and_persists(client):
    resp = await client.post("/api/compliance/check", json={"regulation_id": "reg-1"})
    assert resp.status_code == 200
    events = _events(resp.text)

    assert events[0]["type"] == "summary_init"
    assert events[0]["total"] == 2
    assert events[0]["regulation"]["name"] == "RBI KYC Master Direction"

    rows = {e["row"]["requirement_id"]: e["row"] for e in events if e["type"] == "row"}
    assert len(rows) == 2

    partial = rows["req-1"]
    assert partial["status"] == "Partial"
    assert partial["policy_filename"] == "acme_policy.pdf"
    # Pinecone returned page 2.0 (float); the route normalises it to an int.
    assert partial["policy_page"] == 2
    assert isinstance(partial["policy_page"], int)
    assert partial["rbi_page"] == 3

    gap = rows["req-2"]
    assert gap["status"] == "Gap"
    assert gap["policy_filename"] is None

    final = next(e for e in events if e["type"] == "summary_final")
    assert final["summary"] == {"total": 2, "Covered": 0, "Partial": 1,
                                "Gap": 1, "Conflict": 0, "Needs review": 0}
    assert final["check_id"]
    assert events[-1]["type"] == "DONE"

    # Persisted → listable, and fetchable with the full gap table.
    listed = (await client.get("/api/compliance/checks")).json()["checks"]
    assert len(listed) == 1 and listed[0]["id"] == final["check_id"]

    full = (await client.get(f"/api/compliance/checks/{final['check_id']}")).json()
    assert len(full["rows"]) == 2
    # rows persisted sorted by requirement number
    assert [r["requirement_id"] for r in full["rows"]] == ["req-1", "req-2"]
    assert full["summary"]["Partial"] == 1


@pytest.mark.asyncio
async def test_recheck_only_rejudges_deltas_and_carries_forward(client, monkeypatch):
    # 1. Initial check → persists a prior check (req-1 Partial, req-2 Gap).
    r0 = await client.post("/api/compliance/check", json={"regulation_id": "reg-1"})
    prior_id = next(e for e in _events(r0.text) if e["type"] == "summary_final")["check_id"]

    # 2. A new version of the circular: req-1 unchanged, req-2 reworded (changed),
    #    a brand-new req-3.
    fake_db = app.dependency_overrides[get_db]()      # same shared instance
    fake_db.regs["reg-1"]["requirements"] = [
        {"id": "req-1", "text": "Retain KYC records for at least five years.", "page": 3, "section": None},
        {"id": "req-2", "text": "Identify the beneficial owners and controllers of legal-entity customers.",
         "page": 2, "section": None},
        {"id": "req-3", "text": "File Suspicious Transaction Reports with FIU-IND within seven days.",
         "page": 5, "section": None},
    ]

    # 3. A counting judge proves the unchanged req is NOT re-judged.
    class CountingJudge(FakeJudge):
        calls = 0

        async def ainvoke(self, messages):
            CountingJudge.calls += 1
            return await super().ainvoke(messages)

    monkeypatch.setattr(compliance_router, "build_judge_llm", lambda config, **kw: CountingJudge())

    resp = await client.post("/api/compliance/recheck", json={"check_id": prior_id})
    assert resp.status_code == 200
    events = _events(resp.text)

    init = events[0]
    assert init["type"] == "summary_init"
    assert init["delta"] == {"unchanged": 1, "changed": 1, "added": 1, "removed": 0}
    assert init["total"] == 3                          # unchanged + changed + added; removed isn't a row

    rows = {e["row"]["requirement_id"]: e["row"] for e in events if e["type"] == "row"}
    assert rows["req-1"]["carried_forward"] is True and rows["req-1"]["change"] == "unchanged"
    assert rows["req-1"]["status"] == "Partial"        # prior verdict reused verbatim
    assert rows["req-2"]["carried_forward"] is False and rows["req-2"]["change"] == "changed"
    assert rows["req-3"]["carried_forward"] is False and rows["req-3"]["change"] == "added"

    # Only the 2 deltas were judged — the unchanged req-1 was carried, not re-judged.
    assert CountingJudge.calls == 2

    final = next(e for e in events if e["type"] == "summary_final")
    assert final["delta"] == {"unchanged": 1, "changed": 1, "added": 1, "removed": 0}
    assert final["check_id"]

    # Persisted with the delta + change tags so re-opening shows what changed.
    full = (await client.get(f"/api/compliance/checks/{final['check_id']}")).json()
    assert full["summary"]["delta"]["added"] == 1
    assert {r["requirement_id"] for r in full["rows"]} == {"req-1", "req-2", "req-3"}


@pytest.mark.asyncio
async def test_recheck_with_only_removals_needs_no_judge(client, monkeypatch):
    # A delta-free-of-add/change re-check must succeed even with NO judge key.
    r0 = await client.post("/api/compliance/check", json={"regulation_id": "reg-1"})
    prior_id = next(e for e in _events(r0.text) if e["type"] == "summary_final")["check_id"]

    fake_db = app.dependency_overrides[get_db]()
    fake_db.regs["reg-1"]["requirements"] = [       # req-2 removed; req-1 unchanged
        {"id": "req-1", "text": "Retain KYC records for at least five years.", "page": 3, "section": None},
    ]

    def _raise(config, **kw):                       # judge unconfigured — must not matter
        raise JudgeNotConfigured("no key")
    monkeypatch.setattr(compliance_router, "build_judge_llm", _raise)

    resp = await client.post("/api/compliance/recheck", json={"check_id": prior_id})
    assert resp.status_code == 200
    events = _events(resp.text)
    assert events[0]["delta"] == {"unchanged": 1, "changed": 0, "added": 0, "removed": 1}
    rows = [e["row"] for e in events if e["type"] == "row"]
    assert len(rows) == 1 and rows[0]["requirement_id"] == "req-1"
    assert rows[0]["carried_forward"] is True


@pytest.mark.asyncio
async def test_recheck_unknown_check_returns_404(client):
    resp = await client.post("/api/compliance/recheck", json={"check_id": "nope"})
    assert resp.status_code == 404


# ── User regulation upload ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_regulation_extracts_in_background(client, monkeypatch):
    """A user uploads a circular → 202 + job; the background task parses, extracts
    requirements (the judge), ingests, caches, and the job flips to completed."""
    from src.components.compliance import Requirement

    class _FakeProc:
        def __init__(self, config):
            pass

        def process_documents(self, path):
            return {"pages": [(1, "text")], "filename": "rbi.pdf"}

        def build_langchain_documents(self, parsed, clause_aware=False):
            return ["chunk"]

    async def _fake_extract(chunks, judge):
        return [Requirement(id="req-1", text="Identify with an OVD"),
                Requirement(id="req-2", text="Retain records five years")]

    monkeypatch.setattr(compliance_router, "DocumentProcessor", _FakeProc)
    monkeypatch.setattr(compliance_router, "extract_requirements", _fake_extract)
    monkeypatch.setattr(compliance_router, "build_judge_llm", lambda config, **kw: FakeJudge())

    resp = await client.post(
        "/api/compliance/regulations",
        files={"file": ("rbi.pdf", b"%PDF-1.4 stub", "application/pdf")},
        data={"name": "My RBI Circular", "regulator": "RBI"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    st = (await client.get(f"/api/compliance/regulations/upload-status/{job_id}")).json()
    assert st["status"] == "completed", st
    assert st["requirements"] == 2 and st["regulation_id"]


@pytest.mark.asyncio
async def test_upload_regulation_rejects_bad_extension(client, monkeypatch):
    monkeypatch.setattr(compliance_router, "build_judge_llm", lambda config, **kw: FakeJudge())
    resp = await client.post(
        "/api/compliance/regulations",
        files={"file": ("reg.exe", b"x", "application/octet-stream")},
        data={"name": "X"},
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_upload_regulation_without_judge_returns_503(client, monkeypatch):
    def _raise(config, **kw):
        raise JudgeNotConfigured("CEREBRAS_API_KEY needs to be set")
    monkeypatch.setattr(compliance_router, "build_judge_llm", _raise)
    resp = await client.post(
        "/api/compliance/regulations",
        files={"file": ("rbi.pdf", b"%PDF-1.4 stub", "application/pdf")},
        data={"name": "X"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_upload_regulation_status_scoped_to_owner(client, monkeypatch):
    monkeypatch.setattr(compliance_router, "build_judge_llm", lambda config, **kw: FakeJudge())

    class _FakeProc:
        def __init__(self, config):
            pass

        def process_documents(self, path):
            return {"pages": [(1, "t")]}

        def build_langchain_documents(self, parsed, clause_aware=False):
            return ["chunk"]

    monkeypatch.setattr(compliance_router, "DocumentProcessor", _FakeProc)

    async def _fake_extract(chunks, judge):
        from src.components.compliance import Requirement
        return [Requirement(id="r", text="x")]

    monkeypatch.setattr(compliance_router, "extract_requirements", _fake_extract)

    job_id = (await client.post(
        "/api/compliance/regulations",
        files={"file": ("rbi.pdf", b"%PDF-1.4 stub", "application/pdf")},
        data={"name": "A's circular"},
    )).json()["job_id"]

    _current["id"] = "user-B"  # a different user can't see A's job
    assert (await client.get(f"/api/compliance/regulations/upload-status/{job_id}")).status_code == 404


@pytest.mark.asyncio
async def test_unknown_regulation_returns_404(client):
    resp = await client.post("/api/compliance/check", json={"regulation_id": "nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_regulation_without_requirements_returns_409(client):
    resp = await client.post("/api/compliance/check", json={"regulation_id": "reg-empty"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_judge_not_configured_returns_503(client, monkeypatch):
    def _raise(config, **kw):
        raise JudgeNotConfigured("CEREBRAS_API_KEY needs to be set")
    monkeypatch.setattr(compliance_router, "build_judge_llm", _raise)

    resp = await client.post("/api/compliance/check", json={"regulation_id": "reg-1"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_checks_are_scoped_to_owner(client):
    resp = await client.post("/api/compliance/check", json={"regulation_id": "reg-1"})
    check_id = next(e for e in _events(resp.text) if e["type"] == "summary_final")["check_id"]

    # A different user sees none of A's checks and can't fetch one by id.
    _current["id"] = "user-B"
    assert (await client.get("/api/compliance/checks")).json()["checks"] == []
    assert (await client.get(f"/api/compliance/checks/{check_id}")).status_code == 404
