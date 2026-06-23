"""Regression test for BUG-13 (see BUGFIXES.md).

record_upload stored uploaded_at via datetime.utcnow().isoformat() —
deprecated since Python 3.12, and naive (carries no timezone info, so a
consumer can't tell it's UTC without out-of-band knowledge).
"""

from types import SimpleNamespace

from src.components.config import Config
from src.components.database import SupabaseManager


class _FakeQuery:
    def __init__(self, captured):
        self._captured = captured

    def upsert(self, row, on_conflict=None):
        self._captured.append(row)
        return self

    def execute(self):
        return SimpleNamespace(data=[{"id": 1}])


def test_record_upload_uses_tz_aware_timestamp():
    config = Config(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_ANON_KEY="anon-fake-key",
        SUPABASE_SERVICE_ROLE_KEY="service-fake-key",
    )
    db = SupabaseManager(config)

    captured = []
    db.service_client.table = lambda name: _FakeQuery(captured)

    db.record_upload(user_id="u1", filename="f.pdf", file_type="pdf", size_bytes=100)

    assert len(captured) == 1
    uploaded_at = captured[0]["uploaded_at"]
    assert uploaded_at.endswith("+00:00"), (
        f"expected a tz-aware UTC ISO timestamp (ending in +00:00), got {uploaded_at!r}"
    )
