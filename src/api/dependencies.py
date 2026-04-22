"""dependencies.py — Shared FastAPI dependency-injection helpers."""

from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status

from src.components.config import Config
from src.components.database import SupabaseManager
from src.pipeline.pipeline import RAGPipeline

# ──────────────────────────────────────────────────────────────────────────────
#  Module-level singletons (plain dict avoids lru_cache's hashability
#  requirement — Config is a mutable dataclass and cannot be hashed)
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {}


def get_config() -> Config:
    """Return the global :class:`Config` instance (created once)."""
    if "config" not in _cache:
        _cache["config"] = Config()
    return _cache["config"]


def get_db() -> SupabaseManager:
    """Return the global :class:`SupabaseManager` instance (created once)."""
    if "db" not in _cache:
        _cache["db"] = SupabaseManager(get_config())
    return _cache["db"]


def get_pipeline() -> RAGPipeline:
    """Return the global :class:`RAGPipeline` instance (created once)."""
    if "pipeline" not in _cache:
        _cache["pipeline"] = RAGPipeline(get_config())
    return _cache["pipeline"]


# ──────────────────────────────────────────────────────────────────────────────
#  Auth guard
# ──────────────────────────────────────────────────────────────────────────────


async def get_current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    db: SupabaseManager = Depends(get_db),
) -> dict:
    """Extract and validate the Bearer JWT from the ``Authorization`` header.

    Raises:
        HTTP 401 if the token is missing or invalid.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header (expected: Bearer <token>)",
        )

    token = authorization.split(" ", 1)[1]
    user = db.get_current_user(token)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired.",
        )

    return {"user": user, "access_token": token}
