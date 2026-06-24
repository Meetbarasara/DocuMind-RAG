"""Regression test for BUG-9 (see BUGFIXES.md).

Config.CORS_ORIGINS was a hardcoded default_factory list pointing only at
localhost:8501 — no way to add a deployed frontend's real origin without
editing source code.
"""

from src.components.config import Config


def test_cors_origins_defaults_to_localhost_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Config().CORS_ORIGINS == ["http://localhost:8501"]


def test_cors_origins_reads_comma_separated_env_var(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    assert Config().CORS_ORIGINS == ["https://app.example.com", "https://admin.example.com"]
