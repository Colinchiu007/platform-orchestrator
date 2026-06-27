"""Admin user management router — Phase 2 Membership.

Endpoints:
- GET    /api/admin/users — list users with optional filters and pagination
- GET    /api/admin/users/{uuid} — get user detail with subscription + usage
- PUT    /api/admin/users/{uuid}/status — activate/deactivate user

Uses SQLite get_db for local development. PG support can be added later
by injecting get_db_pg for auth-schema queries while keeping get_db for usage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from pydantic import BaseModel

from db import get_db
from middleware.auth import get_current_user

router = APIRouter()


# ── Request / Response Models ──────────────────────────────────────────


class UserStatusUpdate(BaseModel):
    is_active: bool


class PaginatedUsersResponse(BaseModel):
    users: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int


class UserDetailResponse(BaseModel):
    uuid: str
    username: str
    email: str
    subscription_type: str
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    subscription: Optional[Dict[str, Any]] = None
    usage: List[Dict[str, Any]] = []


# ── Helpers ────────────────────────────────────────────────────────────


def _require_admin(user: Dict[str, Any]) -> None:
    """Check that the authenticated user has admin role."""
    role = user.get("role", "")
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    subscription_type: Optional[str] = Query(
        None, description="Filter by plan: free, basic, pro, enterprise"
    ),
    is_active: Optional[str] = Query(
        None, description='Filter by status: "true" or "false"'
    ),
    search: Optional[str] = Query(None, description="Search by username or email"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_db),
):
    """List users with optional filters and pagination (admin only)."""
    _require_admin(current_user)

    conditions: List[str] = []
    params: List[Any] = []

    # Build WHERE clause dynamically
    if subscription_type:
        conditions.append("u.subscription_type = ?")
        params.append(subscription_type)

    if is_active is not None:
        if is_active.lower() in ("true", "1", "yes"):
            conditions.append("u.is_active = 1")
        elif is_active.lower() in ("false", "0", "no"):
            conditions.append("u.is_active = 0")

    if search:
        conditions.append("(u.username LIKE ? OR u.email LIKE ?)")
        like_pattern = f"%{search}%"
        params.append(like_pattern)
        params.append(like_pattern)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # Count total matching users
    count_sql = f"SELECT COUNT(*) FROM users u {where_clause}"
    async with db.execute(count_sql, params) as cursor:
        row = await cursor.fetchone()
        total = row[0] if row else 0

    # Fetch paginated results
    offset = (page - 1) * page_size
    data_sql = f"""
        SELECT u.uuid, u.username, u.email, u.subscription_type,
               u.is_active, u.created_at, u.updated_at
        FROM users u
        {where_clause}
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
    """
    fetch_params = params + [page_size, offset]
    async with db.execute(data_sql, fetch_params) as cursor:
        rows = await cursor.fetchall()

    users = []
    for row in rows:
        users.append({
            "uuid": row["uuid"],
            "username": row["username"],
            "email": row["email"],
            "subscription_type": row["subscription_type"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

    return PaginatedUsersResponse(
        users=users,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/users/{uuid}")
async def get_user(
    uuid: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get user detail with subscription and usage history (admin only)."""
    _require_admin(current_user)

    # Get user
    async with db.execute(
        "SELECT uuid, username, email, subscription_type, is_active, created_at, updated_at "
        "FROM users WHERE uuid = ?",
        (uuid,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # Get subscription
    async with db.execute(
        "SELECT plan_type, status, start_date, end_date, auto_renew "
        "FROM subscriptions WHERE user_uuid = ?",
        (uuid,),
    ) as cursor:
        sub_row = await cursor.fetchone()

    subscription = None
    if sub_row:
        subscription = {
            "plan_type": sub_row["plan_type"],
            "status": sub_row["status"],
            "start_date": sub_row["start_date"],
            "end_date": sub_row["end_date"],
            "auto_renew": bool(sub_row["auto_renew"]),
        }

    # Get usage history (last 30 days)
    usage = []
    async with db.execute(
        "SELECT date, videos_created, videos_quota "
        "FROM user_daily_usage WHERE user_uuid = ? "
        "ORDER BY date DESC LIMIT 30",
        (uuid,),
    ) as cursor:
        usage_rows = await cursor.fetchall()

    for ur in usage_rows:
        usage.append({
            "date": ur["date"],
            "videos_created": ur["videos_created"],
            "videos_quota": ur["videos_quota"],
        })

    return UserDetailResponse(
        uuid=row["uuid"],
        username=row["username"],
        email=row["email"],
        subscription_type=row["subscription_type"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        subscription=subscription,
        usage=usage,
    )


@router.put("/users/{uuid}/status")
async def toggle_user_status(
    uuid: str,
    body: UserStatusUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_db),
):
    """Activate or deactivate a user (admin only)."""
    _require_admin(current_user)

    # Check user exists
    async with db.execute(
        "SELECT uuid FROM users WHERE uuid = ?",
        (uuid,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    new_status = 1 if body.is_active else 0
    await db.execute(
        "UPDATE users SET is_active = ?, updated_at = datetime('now') WHERE uuid = ?",
        (new_status, uuid),
    )
    await db.commit()

    return {"uuid": uuid, "is_active": body.is_active}
