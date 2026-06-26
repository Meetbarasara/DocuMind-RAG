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

    # ── Semantic cache (C2) ───────────────────────────────────────────────

    @staticmethod
    def _sem_key(namespace: str, filename_filter=None) -> str:
        return f"sem:{namespace}:{filename_filter or '_all'}"

    @staticmethod
    def _cosine(a, b) -> float:
        import numpy as np

        va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(va @ vb / (na * nb))

    def get_semantic(self, namespace: str, query_embedding, filename_filter=None, threshold: float = None):
        """Return a cached answer for a *semantically* near-identical past query.

        Reuses the query embedding to find the closest previously-answered
        question in this namespace+filter; serves it if cosine >= threshold.
        """
        client = self._get_client()
        if client is None:
            return None
        thr = threshold if threshold is not None else self.config.SEMANTIC_CACHE_THRESHOLD
        try:
            best_value, best_sim = None, 0.0
            for raw in client.lrange(self._sem_key(namespace, filename_filter), 0, -1):
                entry = json.loads(raw)
                sim = self._cosine(query_embedding, entry["emb"])
                if sim > best_sim:
                    best_value, best_sim = entry["value"], sim
            return best_value if best_sim >= thr else None
        except Exception as e:
            logger.warning("Semantic cache get failed (treating as miss): %s", e)
            return None

    def add_semantic(self, namespace: str, query_embedding, value: dict, filename_filter=None) -> None:
        """Remember (embedding, answer) so future near-identical queries hit C2."""
        client = self._get_client()
        if client is None:
            return
        try:
            key = self._sem_key(namespace, filename_filter)
            client.lpush(key, json.dumps({"emb": list(query_embedding), "value": value}))
            client.ltrim(key, 0, self.config.SEMANTIC_CACHE_MAX - 1)
            client.expire(key, self.ttl)
        except Exception as e:
            logger.warning("Semantic cache add failed (skipping): %s", e)

    def invalidate(self, namespace: str) -> None:
        """Drop every cached answer (exact + semantic) for *namespace* (C3)."""
        client = self._get_client()
        if client is None:
            return
        try:
            keys = list(client.scan_iter(match=f"qa:{namespace}:*", count=500))
            keys += list(client.scan_iter(match=f"sem:{namespace}:*", count=500))
            if keys:
                client.delete(*keys)
                logger.info("Query cache invalidated %d key(s) for namespace=%s", len(keys), namespace)
        except Exception as e:
            logger.warning("Query cache invalidate failed: %s", e)
