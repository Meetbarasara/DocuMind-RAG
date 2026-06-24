"""auth.py — Authentication routes for DocuMind.

Endpoints:
    POST /api/auth/signup  — create a new user
    POST /api/auth/login   — sign in, return JWT
    POST /api/auth/logout  — invalidate session
    GET  /api/auth/me      — return current user info
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr

from src.api.dependencies import get_current_user, get_db
from src.api.error_utils import log_and_get_ref
from src.api.limiter import limiter
from src.components.database import SupabaseManager
from src.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ──────────────────────────────────────────────────────────────────────────────
#  Request / Response models
# ──────────────────────────────────────────────────────────────────────────────


class AuthRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    message: str
    user_id: str
    email: str
    access_token: str | None = None
    refresh_token: str | None = None


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def signup(request: Request, payload: AuthRequest, db: SupabaseManager = Depends(get_db)):
    """Register a new user account."""
    try:
        # Latency Optimization #7: db.sign_up is a blocking Supabase call.
        result = await asyncio.to_thread(db.sign_up, payload.email, payload.password)
    except Exception as e:
        # SEC-4/SEC-5: raw Supabase error text (e.g. "user already
        # registered" vs. some other failure) used to go straight to the
        # client, which both leaks internals and lets an attacker tell
        # accounts apart. Log the real reason, return a generic message.
        ref = log_and_get_ref(logger, "Sign-up failed", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sign-up failed. (ref: {ref})",
        )

    user = result.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sign-up succeeded but no user was returned.",
        )

    return AuthResponse(
        message="Account created successfully. Check your email for a confirmation link.",
        user_id=str(user.id),
        email=user.email,
    )


@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login(request: Request, payload: AuthRequest, db: SupabaseManager = Depends(get_db)):
    """Authenticate a user and return JWT tokens."""
    try:
        # Latency Optimization #7: db.sign_in is a blocking Supabase call.
        result = await asyncio.to_thread(db.sign_in, payload.email, payload.password)
    except Exception as e:
        # SEC-4/SEC-5: same reasoning as signup — a uniform message means
        # "no such user" and "wrong password" look identical to the client.
        ref = log_and_get_ref(logger, "Login failed", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid email or password. (ref: {ref})",
        )

    user = result.get("user")
    return AuthResponse(
        message="Login successful",
        user_id=str(user.id),
        email=user.email,
        access_token=result.get("access_token"),
        refresh_token=result.get("refresh_token"),
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Invalidate the current user's session."""
    # Latency Optimization #7: db.sign_out is a blocking Supabase call.
    await asyncio.to_thread(db.sign_out, current_user["access_token"])
    return {"message": "Logged out successfully"}


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Return information about the currently authenticated user."""
    user = current_user["user"]
    return {
        "user_id": str(user.id),
        "email": user.email,
        "created_at": str(user.created_at),
    }
