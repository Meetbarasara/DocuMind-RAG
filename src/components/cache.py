"""cache.py — Redis exact-match query cache for DocuMind (C1).

A thin, *fail-open* cache that sits in front of the RAG pipeline. A repeated
question for the same user (namespace) + document filter is served straight
from Redis in a few milliseconds instead of re-running retrieval + the LLM.

Design rules:
  - **Per-namespace keys** (`qa:{namespace}:{hash}`) so one user can never read
    another user's cached answers, and invalidation is scoped per user.
  - **Fail-open**: every method swallows Redis errors and behaves as a miss /
    no-op. A cache problem must never break a query — the pipeline just runs
    normally.
  - **Disabled by default**: no ``REDIS_URL`` => the cache is a no-op, so the
    app runs unchanged until Redis is configured.
  - Invalidation (``invalidate``) is called on every ingest/delete so a user
    never gets a stale answer after changing their documents (C3).
"""

import hashlib
import json

from src.components.config import Config
from src.logger import get_logger

logger = get_logger(__name__)


class QueryCache:
    """Exact-match (per-namespace) answer cache backed by Redis."""

    def __init__(self, config: Config, client=None):
        self.config = config
        self.ttl = config.CACHE_TTL_SECONDS
        # `client` is injected in tests (e.g. fakeredis); otherwise it is
        # connected lazily from REDIS_URL on first use.
        self._client = client
        self._connect_attempted = client is not None

    # ── Connection ────────────────────────────────────────────────────────

    def _get_client(self):
        """Return a live Redis client, or None if unavailable/unconfigured.

        Connects lazily and at most once: a missing/broken Redis disables the
        cache for the process rather than re-paying a failing connect (and its
        timeout) on every query.
        """
        if self._client is not None or self._connect_attempted:
            return self._client

        self._connect_attempted = True
        if not self.config.REDIS_URL:
            return None
        try:
            import redis

            client = redis.from_url(
                self.config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            self._client = client
            logger.info("Query cache connected to Redis.")
        except Exception as e:
            logger.warning("Query cache disabled — Redis unavailable: %s", e)
            self._client = None
        return self._client

    # ── Keys ──────────────────────────────────────────────────────────────

    @staticmethod
    def _key(namespace: str, question: str, filename_filter=None) -> str:
        # Light normalization so trivially-different spellings of the same
        # question share a cache entry; the filter is part of the identity
        # because it changes which chunks are eligible.
        raw = f"{(question or '').strip().lower()}|{filename_filter or ''}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"qa:{namespace}:{digest}"

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, namespace: str, question: str, filename_filter=None):
        """Return the cached answer dict for this query, or None on miss."""
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._key(namespace, question, filename_filter))
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("Query cache get failed (treating as miss): %s", e)
            return None

    def set(self, namespace: str, question: str, value: dict, filename_filter=None) -> None:
        """Store *value* (an answer dict) under this query with the configured TTL."""
        client = self._get_client()
        if client is None:
            return
        try:
            client.set(
                self._key(namespace, question, filename_filter),
                json.dumps(value),
                ex=self.ttl,
            )
        except Exception as e:
            logger.warning("Query cache set failed (skipping): %s", e)

    def invalidate(self, namespace: str) -> None:
        """Drop every cached answer for *namespace* (C3 — on ingest/delete)."""
        client = self._get_client()
        if client is None:
            return
        try:
            keys = list(client.scan_iter(match=f"qa:{namespace}:*", count=500))
            if keys:
                client.delete(*keys)
                logger.info(
                    "Query cache invalidated %d entr%s for namespace=%s",
                    len(keys), "y" if len(keys) == 1 else "ies", namespace,
                )
        except Exception as e:
            logger.warning("Query cache invalidate failed: %s", e)
