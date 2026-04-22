"""auth.py — Authentication routes for DocuMind.

Endpoints:
    POST /api/auth/signup  — create a new user
    POST /api/auth/login   — sign in, return JWT
    POST /api/auth/logout  — invalidate session
    GET  /api/auth/me      — return current user info
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from src.api.dependencies import get_current_user, get_db
from src.components.database import SupabaseManager

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
async def signup(payload: AuthRequest, db: SupabaseManager = Depends(get_db)):
    """Register a new user account."""
    try:
        result = db.sign_up(payload.email, payload.password)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

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
async def login(payload: AuthRequest, db: SupabaseManager = Depends(get_db)):
    """Authenticate a user and return JWT tokens."""
    try:
        result = db.sign_in(payload.email, payload.password)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

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
    db.sign_out(current_user["access_token"])
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
