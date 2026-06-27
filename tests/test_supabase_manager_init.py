"""Regression test for SEC-9 (see BUGFIXES.md).

SupabaseManager silently fell back to the anon client for storage/admin
operations when SUPABASE_SERVICE_ROLE_KEY was missing, only logging a
warning. Combined with SEC-3 (the whole app's user-isolation model
assumes the service-role client is what's actually being used), a missing
env var would silently change the app's security posture instead of
failing loudly at startup where a misconfiguration is easy to notice.
"""

import pytest

from src.components.config import Config
from src.components.database import SupabaseManager
from src.exception import CustomException


def test_missing_service_role_key_fails_fast_instead_of_falling_back_to_anon():
    # Part C: Config itself now refuses to construct with a blank required
    # secret (see test_config_fail_fast.py), so a blank SUPABASE_SERVICE_ROLE_KEY
    # can no longer reach SupabaseManager via the constructor. This exercises
    # SupabaseManager's own check as defense-in-depth against a config object
    # that went blank after construction (e.g. a careless re-assignment).
    config = Config(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_ANON_KEY="anon-fake-key",
        SUPABASE_SERVICE_ROLE_KEY="service-fake-key",
    )
    config.SUPABASE_SERVICE_ROLE_KEY = ""

    with pytest.raises(CustomException):
        SupabaseManager(config)


def test_service_role_key_present_constructs_normally():
    config = Config(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_ANON_KEY="anon-fake-key",
        SUPABASE_SERVICE_ROLE_KEY="service-fake-key",
    )

    db = SupabaseManager(config)

    assert db.client is not db.service_client
