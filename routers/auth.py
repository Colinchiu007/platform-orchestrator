"""Authentication router — register, login, token refresh.

Uses passlib for password hashing and the JWT middleware from middleware/auth.py.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from config import settings
from db import get_db
from middleware.auth import create_access_token, decode_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Request / Response Models ───────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr = Field(...)
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(...)
    password: str = Field(...)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db=Depends(get_db)):
    """Register a new user account."""
    # Check if username or email already exists
    async with db.execute(
        "SELECT id FROM users WHERE username = ? OR email = ?",
        (body.username, body.email),
    ) as cursor:
        if await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email already registered",
            )

    user_uuid = str(uuid.uuid4())
    password_hash = _hash_password(body.password)

    await db.execute(
        """INSERT INTO users (uuid, username, email, password_hash, subscription_type)
           VALUES (?, ?, ?, ?, 'free')""",
        (user_uuid, body.username, body.email, password_hash),
    )
    await db.commit()

    return {
        "id": user_uuid,
        "username": body.username,
        "email": body.email,
        "subscription_type": "free",
    }


@router.post("/login")
async def login(body: LoginRequest, db=Depends(get_db)):
    """Login and receive JWT tokens."""
    async with db.execute(
        "SELECT uuid, username, password_hash, subscription_type FROM users WHERE username = ?",
        (body.username,),
    ) as cursor:
        user = await cursor.fetchone()

    if not user or not _verify_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Map subscription_type to tier (basic=1, premium=2)
    tier_map = {"free": 1, "basic": 1, "pro": 2, "enterprise": 2}
    tier = tier_map.get(user["subscription_type"], 1)

    access_token = create_access_token(
        data={
            "sub": user["uuid"],
            "username": user["username"],
            "tier": tier,
        }
    )
    refresh_token = create_access_token(
        data={"sub": user["uuid"], "type": "refresh"},
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user={
            "id": user["uuid"],
            "username": user["username"],
            "subscription_type": user["subscription_type"],
            "tier": tier,
        },
    )


@router.post("/refresh")
async def refresh_token(body: RefreshRequest):
    """Refresh access token using refresh token."""
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    # TODO: In production, verify refresh token against DB store
    access_token = create_access_token(
        data={"sub": payload["sub"], "username": payload.get("username", ""), "tier": payload.get("tier", 1)}
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Get current user profile."""
    async with db.execute(
        "SELECT uuid, username, email, subscription_type, created_at FROM users WHERE uuid = ?",
        (current_user["sub"],),
    ) as cursor:
        user = await cursor.fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return dict(user)
