"""limiter.py — shared slowapi Limiter instance.

Lives in its own module rather than main.py so router files can import it
and apply @limiter.limit(...) directly. main.py imports the routers, so the
routers can't import the limiter back out of main.py without a circular
import (SEC-7's actual root cause — see BUGFIXES.md).

Storage: in-memory by default (correct for a single worker). When REDIS_URL is
set, the counters live in Redis so the limit holds ACROSS worker processes —
otherwise each worker keeps its own counters and the effective limit is N times
looser under N workers. Fail-open: if Redis storage can't be initialised, fall
back to in-memory rather than break startup.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_LIMITS = ["60/minute"]


def _build_limiter(redis_url: str = "") -> Limiter:
    """Build the shared limiter, backed by Redis when *redis_url* is set."""
    redis_url = (redis_url or "").strip()
    if redis_url:
        try:
            lim = Limiter(
                key_func=get_remote_address,
                default_limits=_DEFAULT_LIMITS,
                storage_uri=redis_url,
            )
            logger.info("Rate limiter using shared Redis storage (multi-worker-safe).")
            return lim
        except Exception as e:
            logger.warning(
                "Redis rate-limit storage unavailable (%s) — using in-memory "
                "(correct only for a single worker).", e,
            )
    return Limiter(key_func=get_remote_address, default_limits=_DEFAULT_LIMITS)


limiter = _build_limiter(os.getenv("REDIS_URL", ""))
