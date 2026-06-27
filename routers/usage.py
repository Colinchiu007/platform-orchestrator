"""Usage tracking router — daily quota endpoints.

Endpoints:
- GET /api/user/usage — returns current daily usage (videos_used, quota, reset_time)
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from db_pg import get_db_pg as get_db_pg_session
from middleware.auth import get_current_user
from models.auth_models import AuthUser
from sqlalchemy import select

from services.quota import get_usage, get_quota

router = APIRouter()


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD in local time."""
    return datetime.now().strftime("%Y-%m-%d")


def _reset_time_str() -> str:
    """Return today's reset time (midnight of the next day) as ISO string."""
    tomorrow = datetime.combine(
        datetime.now().date(),
        time.min,
    ).replace(hour=0, minute=0, second=0)
    # This is the start of tomorrow = end of today
    return tomorrow.isoformat()


@router.get("/usage")
async def get_user_usage(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_db),
    pg_db: AsyncSession = Depends(get_db_pg_session),
):
    """Get current daily usage for the authenticated user.

    Returns:
    - videos_used: number of videos created today
    - videos_quota: daily limit for the user's plan
    - reset_time: ISO datetime when the quota resets
    - plan_type: current subscription plan
    """
    user_uuid = current_user["sub"]
    today = _today_str()

    # Get user's subscription type from PostgreSQL
    result = await pg_db.execute(
        select(AuthUser).where(AuthUser.uuid == user_uuid)
    )
    user = result.scalar_one_or_none()
    plan_type = user.subscription_type if user else "free"

    # Get or create usage row from SQLite
    usage = await get_usage(db, user_uuid, today)

    # If no row exists yet, use the plan's default quota
    quota = usage["videos_quota"] if usage["videos_quota"] > 0 else get_quota(plan_type)

    return {
        "videos_used": usage["videos_created"],
        "videos_quota": quota,
        "reset_time": _reset_time_str(),
        "plan_type": plan_type,
        "date": today,
    }
