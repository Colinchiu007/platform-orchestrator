"""Authentication router — register, login, token refresh.

Uses passlib for password hashing and the JWT middleware from middleware/auth.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from config import settings
from db import get_db
from middleware.auth import create_access_token, decode_token, get_current_user
from middleware.feature_gate import requires_feature
from middleware.rate_limit import limiter

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
    access_token: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("3/hour")
async def register(request: Request, body: RegisterRequest, db=Depends(get_db)):
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
@limiter.limit("5/minute")
async def login(
    request: Request, body: LoginRequest, db=Depends(get_db),
    response: Response = None,
):
    """Login and receive JWT tokens."""
    sql = (
        "SELECT uuid, username, password_hash, subscription_type "
        "FROM users WHERE username = ?"
    )
    async with db.execute(sql, (body.username,),) as cursor:
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
    refresh_token_jti = str(uuid.uuid4())
    refresh_token = create_access_token(
        data={"sub": user["uuid"], "type": "refresh", "jti": refresh_token_jti},
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )
    expires_at = (
        datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days)
    ).isoformat()

    await db.execute(
        """INSERT INTO refresh_tokens (token_jti, user_uuid, expires_at)
           VALUES (?, ?, ?)""",
        (refresh_token_jti, user["uuid"], expires_at),
    )
    await db.commit()

    # Set HttpOnly cookie for web clients
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,  # False for localhost/dev; set True in production behind HTTPS
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
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
async def refresh_token(body: RefreshRequest, db=Depends(get_db)):
    """Refresh access token using refresh token.

    Validates the token against the refresh_tokens table:
    - Must exist (not been pruned or never stored)
    - Must not be revoked (logout invalidates it)
    """
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token: missing jti",
        )

    async with db.execute(
        "SELECT token_jti, revoked, expires_at FROM refresh_tokens WHERE token_jti = ?",
        (jti,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row or row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or not found",
        )

    # Defense-in-depth: verify DB expiration even though JWT also enforces exp
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )

    access_token = create_access_token(
        data={
            "sub": payload["sub"],
            "username": payload.get("username", ""),
            "tier": payload.get("tier", 1),
        },
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/logout")
async def logout(body: LogoutRequest, db=Depends(get_db), response: Response = None):
    """Revoke a refresh token — subsequent refresh attempts will fail."""
    try:
        payload = decode_token(body.refresh_token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid refresh token",
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token missing jti",
        )

    await db.execute(
        "UPDATE refresh_tokens SET revoked = 1 WHERE token_jti = ?",
        (jti,),
    )
    await db.commit()

    # Clear the access_token cookie
    response.delete_cookie(key="access_token", httponly=True, samesite="lax")

    return {"detail": "Token revoked"}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Get current user profile."""
    sql = (
        "SELECT uuid, username, email, subscription_type, created_at "
        "FROM users WHERE uuid = ?"
    )
    async with db.execute(sql, (current_user["sub"],),) as cursor:
        user = await cursor.fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return dict(user)


# ── Subscription Models ─────────────────────────────────────────────────


class UpgradeRequest(BaseModel):
    plan: Literal["free", "basic", "pro", "enterprise"]


# ── Subscription Data ───────────────────────────────────────────────────

FEATURES_MAP = {
    "free": ["articles", "basic_split"],
    "basic": ["articles", "basic_split", "batch_split"],
    "pro": [
        "articles", "basic_split", "batch_split",
        "voice_clone", "video_fixed_template",
    ],
    "enterprise": [
        "articles", "basic_split", "batch_split",
        "voice_clone", "video_fixed_template",
    ],
}


# ── Subscription Endpoints ──────────────────────────────────────────────


@router.post("/upgrade")
async def upgrade(
    body: UpgradeRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Upgrade (or downgrade) the current user's subscription plan."""
    # Verify user exists in DB
    async with db.execute(
        "SELECT uuid, subscription_type FROM users WHERE uuid = ?",
        (current_user["sub"],),
    ) as cursor:
        user = await cursor.fetchone()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    tier_map = {"free": 1, "basic": 1, "pro": 2, "enterprise": 2}

    # Update users.subscription_type
    sql = (
        "UPDATE users SET subscription_type = ?, "
        "updated_at = datetime('now') WHERE uuid = ?"
    )
    await db.execute(sql, (body.plan, current_user["sub"]),)

    # Upsert subscriptions row
    now = datetime.utcnow().isoformat()
    end_date = (datetime.utcnow() + timedelta(days=30)).isoformat()
    sql = (
        "INSERT INTO subscriptions "
        "(user_uuid, plan_type, status, start_date, end_date, auto_renew) "
        "VALUES (?, ?, 'active', ?, ?, 1) "
        "ON CONFLICT(user_uuid) DO UPDATE SET "
        "plan_type = excluded.plan_type, "
        "status = 'active', "
        "start_date = excluded.start_date, "
        "end_date = excluded.end_date, "
        "auto_renew = 1"
    )
    await db.execute(sql, (current_user["sub"], body.plan, now, end_date),)
    await db.commit()

    return {
        "plan_type": body.plan,
        "status": "active",
        "tier": tier_map.get(body.plan, 1),
    }


@router.get("/subscription")
async def get_subscription(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get the current user's subscription details and features."""
    async with db.execute(
        "SELECT subscription_type FROM users WHERE uuid = ?",
        (current_user["sub"],),
    ) as cursor:
        user = await cursor.fetchone()

    plan_type = user["subscription_type"] if user else "free"

    # Look up subscriptions table for dates/status
    async with db.execute(
        "SELECT status, start_date, end_date FROM subscriptions WHERE user_uuid = ?",
        (current_user["sub"],),
    ) as cursor:
        sub = await cursor.fetchone()

    return {
        "plan_type": plan_type,
        "features": FEATURES_MAP.get(plan_type, FEATURES_MAP["free"]),
        "status": sub["status"] if sub else "active",
        "start_date": sub["start_date"] if sub else None,
        "end_date": sub["end_date"] if sub else None,
    }


@router.get("/premium-content")
@requires_feature("premium_content")
async def premium_content(
    current_user: dict = Depends(get_current_user),
):
    """Test endpoint — only users with tier >= 2 can access."""
    return {"content": "Premium content unlocked"}
