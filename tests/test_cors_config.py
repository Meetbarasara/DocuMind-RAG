"""Regression tests for BUG-9 and the LAN-origin "Failed to fetch" fix
(see BUGFIXES.md).

Config.CORS_ORIGINS was a hardcoded default_factory list pointing only at
localhost:8501 — no way to add a deployed frontend's real origin without
editing source code. CORS_ORIGIN_REGEX complements the list: a fixed list
can't cover DHCP-assigned LAN IPs (http://10.x.x.x:3000).
"""

import re

from src.components.config import Config


def test_cors_origins_defaults_to_localhost_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Config().CORS_ORIGINS == ["http://localhost:8501"]


def test_cors_origins_reads_comma_separated_env_var(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    assert Config().CORS_ORIGINS == ["https://app.example.com", "https://admin.example.com"]


def test_cors_origin_regex_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("CORS_ORIGIN_REGEX", raising=False)
    assert Config().CORS_ORIGIN_REGEX is None


def test_cors_origin_regex_reads_env_and_matches_lan_origins(monkeypatch):
    # The documented .env.example pattern: private-range IPs, any port.
    pattern = (
        r"^http://(localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
    )
    monkeypatch.setenv("CORS_ORIGIN_REGEX", pattern)
    cfg = Config()
    assert cfg.CORS_ORIGIN_REGEX == pattern
    # Starlette matches the Origin header with re.fullmatch.
    assert re.fullmatch(cfg.CORS_ORIGIN_REGEX, "http://10.200.3.54:3000")
    assert re.fullmatch(cfg.CORS_ORIGIN_REGEX, "http://192.168.1.7:3000")
    assert not re.fullmatch(cfg.CORS_ORIGIN_REGEX, "http://evil.example.com")


def test_app_passes_origin_regex_to_cors_middleware():
    from fastapi.middleware.cors import CORSMiddleware

    from src.api.main import app

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert "allow_origin_regex" in cors.kwargs
