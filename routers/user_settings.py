"""User settings router — profile and API key management.

Endpoints:
- GET   /api/settings/profile — get current user profile
- PATCH /api/settings/profile — update profile (username/email)
- GET   /api/settings/api-keys — list user API keys
- POST  /api/settings/api-keys — create a new API key
- DELETE /api/settings/api-keys/{key_id} — delete an API key

Profile data lives in PostgreSQL (auth schema).
API keys live in SQLite (api_keys table).
"""

from __future__ import annotations

import json
import secrets
import uuid as uuid_mod
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import get_db as get_sqlite_db
from db_pg import get_db_pg
from middleware.auth import get_current_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models.auth_models import AuthUser

logger = __import__("logging").getLogger(__name__)

router = APIRouter()


# ── Profile ──────────────────────────────────────────────────────


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = Field(default=None, max_length=50)
    email: Optional[str] = Field(default=None, max_length=255)


@router.get("/profile")
async def get_profile(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_pg),
):
    """Get current user profile.
    
    Returns UserProfile-compatible response for unified-frontend.
    Maps subscription_type to role field.
    """
    result = await db.execute(
        select(AuthUser).where(AuthUser.uuid == current_user["sub"])
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": user.uuid,
        "username": user.username,
        "email": user.email,
        "role": user.subscription_type or "free",
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_pg),
):
    """Update current user profile.
    
    Only username and email can be updated via this endpoint.
    """
    result = await db.execute(
        select(AuthUser).where(AuthUser.uuid == current_user["sub"])
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.username is not None:
        # Check uniqueness
        dup = await db.execute(
            select(AuthUser).where(
                AuthUser.username == body.username,
                AuthUser.uuid != current_user["sub"],
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")
        user.username = body.username

    if body.email is not None:
        dup = await db.execute(
            select(AuthUser).where(
                AuthUser.email == body.email,
                AuthUser.uuid != current_user["sub"],
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")
        user.email = body.email

    await db.flush()

    return {
        "id": user.uuid,
        "username": user.username,
        "email": user.email,
        "role": user.subscription_type or "free",
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ── API Keys ─────────────────────────────────────────────────────


class CreateApiKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=100, description="Human-readable label for this key")


@router.get("/api-keys")
async def list_api_keys(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_sqlite_db),
):
    """List all API keys for the current user.
    
    Returns key previews (last 8 chars), not full keys.
    """
    async with db.execute(
        """SELECT id, label, key_preview, created_at, last_used_at
           FROM api_keys WHERE user_id = ?
           ORDER BY created_at DESC""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()

    return {"items": [dict(r) for r in rows]}


@router.post("/api-keys", status_code=201)
async def create_api_key(
    body: CreateApiKeyRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_sqlite_db),
):
    """Create a new API key.
    
    Generates a random hex key. The full key is returned only once
    at creation time. Only the last 8 chars are stored for preview.
    """
    key_id = str(uuid_mod.uuid4())
    raw_key = secrets.token_hex(32)  # 64-char hex key
    key_preview = "..." + raw_key[-8:]

    await db.execute(
        """INSERT INTO api_keys (id, user_id, label, key_hash, key_preview)
           VALUES (?, ?, ?, ?, ?)""",
        (key_id, current_user["sub"], body.label, raw_key, key_preview),
    )
    await db.commit()

    return {
        "id": key_id,
        "label": body.label,
        "key": raw_key,
        "key_preview": key_preview,
    }


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(
    key_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_sqlite_db),
):
    """Delete an API key by ID."""
    async with db.execute(
        "SELECT id FROM api_keys WHERE id = ? AND user_id = ?",
        (key_id, current_user["sub"]),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="API key not found")

    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()
    return None
