"""Regression test for BUG-2 (see BUGFIXES.md).

sign_out() called self.client.auth.admin.sign_out(...) — but self.client is
the anon-key client, and admin.* operations require the service-role
client. Confirmed via SDK inspection (supabase 2.28.3 / supabase_auth):
admin.sign_out(jwt: str, scope: SignOutScope = "global") takes the user's
JWT, not a user id as the original review speculated — the actual bug is
purely "wrong client instance," not a signature mismatch.
"""

from src.components.config import Config
from src.components.database import SupabaseManager


def make_db():
    config = Config(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_ANON_KEY="anon-fake-key",
        SUPABASE_SERVICE_ROLE_KEY="service-fake-key",
    )
    return SupabaseManager(config)


def test_sign_out_uses_service_client_not_anon():
    db = make_db()
    assert db.client is not db.service_client, "test needs two distinct clients to be meaningful"

    calls = []

    def fake_sign_out_anon(jwt, scope="global"):
        calls.append(("anon", jwt))
        raise Exception("admin.sign_out requires the service-role client, not anon")

    def fake_sign_out_service(jwt, scope="global"):
        calls.append(("service", jwt))

    db.client.auth.admin.sign_out = fake_sign_out_anon
    db.service_client.auth.admin.sign_out = fake_sign_out_service

    result = db.sign_out("user-access-token-123")

    assert result is True, "sign_out should succeed when called on the correct (service) client"
    assert calls == [("service", "user-access-token-123")], (
        f"expected exactly one call, to the service client, got: {calls}"
    )
