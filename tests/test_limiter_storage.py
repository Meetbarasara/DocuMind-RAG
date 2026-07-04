"""The shared rate limiter uses Redis storage when REDIS_URL is set (so the limit
holds across worker processes), and fails open to in-memory otherwise."""

import src.api.limiter as limiter_module
from src.api.limiter import _build_limiter


def test_in_memory_without_redis_url():
    lim = _build_limiter("")
    assert lim is not None
    # a real Limiter, constructed without a storage_uri
    assert type(lim).__name__ == "Limiter"


def test_uses_redis_storage_when_url_set(monkeypatch):
    seen = {}

    class _FakeLimiter:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(limiter_module, "Limiter", _FakeLimiter)
    _build_limiter("redis://localhost:6379/0")
    assert seen.get("storage_uri") == "redis://localhost:6379/0"


def test_falls_back_to_memory_when_redis_backend_missing(monkeypatch):
    calls = []

    class _FakeLimiter:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("storage_uri"):          # simulate "no redis backend installed"
                raise RuntimeError("redis storage backend unavailable")

    monkeypatch.setattr(limiter_module, "Limiter", _FakeLimiter)
    _build_limiter("redis://x")
    # tried Redis first, then built an in-memory limiter (no storage_uri)
    assert len(calls) == 2
    assert "storage_uri" not in calls[1] or not calls[1]["storage_uri"]
