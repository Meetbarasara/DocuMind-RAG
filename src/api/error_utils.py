"""error_utils.py — safe error responses (SEC-4).

Route handlers (and the streaming pipeline) were putting raw exception
text straight into client-facing responses — `detail=str(e)`, f"...{e}",
or an SSE error event's `message`. That can leak stack/provider error
text or internal hostnames, and made signup/login error text revealing
enough to enable user-enumeration (SEC-5).

log_and_get_ref() logs the real exception server-side with a short
reference id and returns just that id, so the caller can build a generic
client-facing message while the real detail stays in the logs for
debugging — the id ties the two together.
"""

import logging
import uuid


def log_and_get_ref(logger: logging.Logger, public_message: str, exc: Exception) -> str:
    error_id = uuid.uuid4().hex[:8]
    logger.error("[%s] %s: %s", error_id, public_message, exc, exc_info=True)
    return error_id
