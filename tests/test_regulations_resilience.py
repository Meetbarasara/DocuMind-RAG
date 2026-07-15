"""Regression tests for the WinError-10035 "No regulations yet" bug (BUGFIXES).

A transient socket error (WSAEWOULDBLOCK from the shared sync HTTP client under
concurrent requests on Windows) was swallowed by list_regulations into [], so
the UI confidently rendered an empty state while the table held seeded data.
The db layer must retry transient blips and RAISE when the store is really
unreachable; the route must answer 503, never 200-[].
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db
from src.api.main import app
from src.components.database import SupabaseManager, _is_transient_net_error
from src.exception import CustomException

WSAEWOULDBLOCK = OSError(10035, "A non-blocking socket operation could not be completed immediately")


class _FlakyQuery:
    """Stands in for the supabase table-query chain; fails the first
    ``failures`` execute() calls with the given exception, then returns rows."""

    def __init__(self, failures: int, exc: Exception, rows):
        self.failures = failures
        self.exc = exc
        self.rows = rows
        self.calls = 0

    def table(self, name):
        return self

    select = order = eq = limit = lambda self, *a, **k: self  # noqa: E731 — chain no-ops

    def execute(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return SimpleNamespace(data=self.rows)


def _manager(service_client) -> SupabaseManager:
    # __new__ skips __init__ (which builds real clients); these methods only
    # touch self.service_client.
    mgr = SupabaseManager.__new__(SupabaseManager)
    mgr.service_client = service_client
    return mgr


ROWS = [{"id": "reg-1", "name": "RBI KYC (synthetic demo)"}]


def test_transient_blip_is_retried_and_rows_returned():
    q = _FlakyQuery(failures=2, exc=WSAEWOULDBLOCK, rows=ROWS)
    assert _manager(q).list_regulations() == ROWS
    assert q.calls == 3


def test_exhausted_transient_raises_instead_of_returning_empty():
    q = _FlakyQuery(failures=99, exc=WSAEWOULDBLOCK, rows=ROWS)
    with pytest.raises(CustomException):
        _manager(q).list_regulations()  # pre-fix: returned [] and hid the outage


def test_get_regulation_retries_transient_and_keeps_none_for_missing():
    flaky = _FlakyQuery(failures=1, exc=WSAEWOULDBLOCK, rows=[{"id": "reg-1"}])
    assert _manager(flaky).get_regulation("reg-1") == {"id": "reg-1"}
    empty = _FlakyQuery(failures=0, exc=WSAEWOULDBLOCK, rows=[])
    assert _manager(empty).get_regulation("nope") is None  # genuine absence → None


def test_non_transient_errors_are_not_retried():
    q = _FlakyQuery(failures=99, exc=ValueError("bad column"), rows=ROWS)
    with pytest.raises(CustomException):
        _manager(q).list_regulations()
    assert q.calls == 1


def test_transient_detection_walks_wrapped_exceptions():
    wrapped = RuntimeError("client call failed")
    wrapped.__cause__ = WSAEWOULDBLOCK
    assert _is_transient_net_error(wrapped)
    assert not _is_transient_net_error(ValueError("bad column"))


@pytest.mark.asyncio
async def test_route_returns_503_not_empty_list_when_store_unreachable():
    class _DownDb:
        def list_regulations(self):
            raise CustomException("Could not list regulations: [WinError 10035] ...")

    async def _user():
        return {"user": SimpleNamespace(id="user-A"), "access_token": "t"}

    app.dependency_overrides[get_db] = lambda: _DownDb()
    app.dependency_overrides[get_current_user] = _user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            res = await c.get("/api/compliance/regulations")
        assert res.status_code == 503
        assert "Could not load regulations" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
