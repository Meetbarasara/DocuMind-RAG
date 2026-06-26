"""C1: Redis exact-match query cache (src/components/cache.py).

Uses fakeredis (in-memory Redis emulation) so these run without a real Redis
server. Covers the cache contract: hit/miss, light normalization, per-namespace
isolation, filter sensitivity, invalidation, and fail-open behavior when Redis
is absent or erroring (a cache problem must never break a query).
"""

import fakeredis

from src.components.cache import QueryCache
from src.components.config import Config


def _cache(ttl=3600):
    client = fakeredis.FakeRedis(decode_responses=True)
    return QueryCache(Config(CACHE_TTL_SECONDS=ttl), client=client)


def test_set_then_get_round_trips():
    c = _cache()
    assert c.get("ns1", "What is X?") is None
    c.set("ns1", "What is X?", {"answer": "X is a thing", "sources": []})
    assert c.get("ns1", "What is X?")["answer"] == "X is a thing"


def test_light_normalization_hits_same_entry():
    c = _cache()
    c.set("ns1", "What is X?", {"answer": "a"})
    assert c.get("ns1", "  what is x?  ")["answer"] == "a"


def test_namespace_isolation():
    c = _cache()
    c.set("ns1", "q", {"answer": "secret"})
    assert c.get("ns2", "q") is None  # another user must not read it


def test_filename_filter_is_part_of_identity():
    c = _cache()
    c.set("ns1", "q", {"answer": "all"}, filename_filter=None)
    assert c.get("ns1", "q", filename_filter="report.pdf") is None


def test_invalidate_clears_only_that_namespace():
    c = _cache()
    c.set("ns1", "q1", {"answer": "1"})
    c.set("ns1", "q2", {"answer": "2"})
    c.set("ns2", "q1", {"answer": "other"})

    c.invalidate("ns1")

    assert c.get("ns1", "q1") is None
    assert c.get("ns1", "q2") is None
    assert c.get("ns2", "q1")["answer"] == "other"  # untouched


def test_disabled_when_no_redis_url_is_a_noop():
    c = QueryCache(Config(REDIS_URL=""))  # no injected client, no URL
    assert c.get("ns1", "q") is None
    c.set("ns1", "q", {"answer": "a"})    # must not raise
    c.invalidate("ns1")                   # must not raise
    assert c.get("ns1", "q") is None


def test_fail_open_when_redis_errors():
    class BoomClient:
        def get(self, *a, **k):
            raise RuntimeError("redis down")

        def setex(self, *a, **k):
            raise RuntimeError("redis down")

        def scan_iter(self, *a, **k):
            raise RuntimeError("redis down")

    c = QueryCache(Config(), client=BoomClient())
    assert c.get("ns1", "q") is None     # swallowed -> miss
    c.set("ns1", "q", {"answer": "a"})   # swallowed -> no raise
    c.invalidate("ns1")                  # swallowed -> no raise
