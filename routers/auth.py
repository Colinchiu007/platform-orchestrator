"""Authentication router — register, login, token refresh (Phase B: PG via SQLAlchemy).

Uses SQLAlchemy ORM with PostgreSQL auth schema.
Shared PG instance with trendscope, schema 'auth'.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from shared_models.auth import (
    AuthTokenResponse,
    JWTPayload,
    LoginRequest,
    RefreshRequest as SharedRefreshRequest,
    RegisterRequest,
)
RefreshRequest = SharedRefreshRequest  # noqa: F811  — use shared-models Pydantic model
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_pg import get_db_pg
from middleware.auth import create_access_token, decode_token, get_current_user
from middleware.feature_gate import requires_feature
from middleware.rate_limit import limiter
from models.auth_models import AuthUser, AuthRefreshToken, AuthSubscription

router = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Request / Response Models ───────────────────────────────────────────────


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
async def register(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db_pg),
):
    """Register a new user account."""
    # Check if username or email already exists
    existing = await db.execute(
        select(AuthUser).where(
            (AuthUser.username == body.username) | (AuthUser.email == body.email)
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered",
        )

    user_uuid = str(uuid.uuid4())
    user = AuthUser(
        uuid=user_uuid,
        username=body.username,
        email=body.email,
        password_hash=_hash_password(body.password),
    )
    db.add(user)
    await db.flush()

    return {
        "id": user_uuid,
        "username": body.username,
        "email": body.email,
        "subscription_type": "free",
    }


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db_pg),
    response: Response = None,
):
    """Login and receive JWT tokens."""
    result = await db.execute(
        select(AuthUser).where(AuthUser.username == body.username)
    )
    user = result.scalar_one_or_none()

    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    tier_map = {"free": 1, "basic": 1, "pro": 2, "enterprise": 2}
    tier = tier_map.get(user.subscription_type, 1)

    access_token = create_access_token(
        data={
            "sub": user.uuid,
            "username": user.username,
            "tier": tier,
        }
    )
    refresh_token_jti = str(uuid.uuid4())
    refresh_token = create_access_token(
        data={"sub": user.uuid, "type": "refresh", "jti": refresh_token_jti},
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )
    expires_at = datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days)

    rt = AuthRefreshToken(
        token_jti=refresh_token_jti,
        user_uuid=user.uuid,
        expires_at=expires_at,
    )
    db.add(rt)
    await db.flush()

    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )

    return AuthTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user={
            "id": user.uuid,
            "username": user.username,
            "subscription_type": user.subscription_type,
            "tier": tier,
        },
    )


@router.post("/refresh")
async def refresh_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db_pg),
):
    """Refresh access token using refresh token."""
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

    result = await db.execute(
        select(AuthRefreshToken).where(
            AuthRefreshToken.token_jti == jti,
            AuthRefreshToken.revoked == False,
        )
    )
    row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or not found",
        )

    if row.expires_at < datetime.utcnow():
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
async def logout(
    body: LogoutRequest,
    db: AsyncSession = Depends(get_db_pg),
    response: Response = None,
):
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
        update(AuthRefreshToken)
        .where(AuthRefreshToken.token_jti == jti)
        .values(revoked=True)
    )
    await db.flush()

    response.delete_cookie(key="access_token", httponly=True, samesite="lax")

    return {"detail": "Token revoked"}


@router.get("/me")
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_pg),
):
    """Get current user profile."""
    result = await db.execute(
        select(AuthUser).where(AuthUser.uuid == current_user["sub"])
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "uuid": user.uuid,
        "username": user.username,
        "email": user.email,
        "subscription_type": user.subscription_type,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


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
    db: AsyncSession = Depends(get_db_pg),
):
    """Upgrade (or downgrade) the current user's subscription plan."""
    result = await db.execute(
        select(AuthUser).where(AuthUser.uuid == current_user["sub"])
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    tier_map = {"free": 1, "basic": 1, "pro": 2, "enterprise": 2}

    user.subscription_type = body.plan

    now = datetime.utcnow()
    end_date = now + timedelta(days=30)

    # Upsert subscription row
    sub_result = await db.execute(
        select(AuthSubscription).where(AuthSubscription.user_uuid == current_user["sub"])
    )
    sub = sub_result.scalar_one_or_none()
    if sub:
        sub.plan_type = body.plan
        sub.status = "active"
        sub.start_date = now
        sub.end_date = end_date
        sub.auto_renew = True
    else:
        db.add(AuthSubscription(
            user_uuid=current_user["sub"],
            plan_type=body.plan,
            status="active",
            start_date=now,
            end_date=end_date,
            auto_renew=True,
        ))
    await db.flush()

    return {
        "plan_type": body.plan,
        "status": "active",
        "tier": tier_map.get(body.plan, 1),
    }


@router.get("/subscription")
async def get_subscription(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_pg),
):
    """Get the current user's subscription details and features."""
    result = await db.execute(
        select(AuthUser).where(AuthUser.uuid == current_user["sub"])
    )
    user = result.scalar_one_or_none()

    plan_type = user.subscription_type if user else "free"

    sub_result = await db.execute(
        select(AuthSubscription).where(AuthSubscription.user_uuid == current_user["sub"])
    )
    sub = sub_result.scalar_one_or_none()

    return {
        "plan_type": plan_type,
        "features": FEATURES_MAP.get(plan_type, FEATURES_MAP["free"]),
        "status": sub.status if sub else "active",
        "start_date": sub.start_date.isoformat() if sub and sub.start_date else None,
        "end_date": sub.end_date.isoformat() if sub and sub.end_date else None,
    }


@router.get("/premium-content")
@requires_feature("premium_content")
async def premium_content(
    current_user: dict = Depends(get_current_user),
):
    """Test endpoint — only users with tier >= 2 can access."""
    return {"content": "Premium content unlocked"}
