"""Quota service — daily usage tracking and enforcement.

Provides:
- get_usage(db, user_uuid, date) — get or create today's usage row
- increment_usage(db, user_uuid, date) — atomically increment + check quota
- get_quota(user_subscription_type) — returns daily video quota based on plan
"""

from __future__ import annotations

from typing import Dict, Any, Optional

QUOTA_MAP: Dict[str, int] = {
    "free": 3,
    "basic": 10,
    "pro": 50,
    "enterprise": 200,
}


def get_quota(subscription_type: str) -> int:
    """Return daily video quota for the given subscription plan."""
    return QUOTA_MAP.get(subscription_type, QUOTA_MAP["free"])


async def get_usage(db, user_uuid: str, date: str) -> Dict[str, Any]:
    """Get or create today's usage row for a user.

    Returns dict with: user_uuid, date, videos_created, videos_quota
    """
    async with db.execute(
        "SELECT user_uuid, date, videos_created, videos_quota "
        "FROM user_daily_usage WHERE user_uuid = ? AND date = ?",
        (user_uuid, date),
    ) as cursor:
        row = await cursor.fetchone()

    if row:
        return {
            "user_uuid": row["user_uuid"],
            "date": row["date"],
            "videos_created": row["videos_created"],
            "videos_quota": row["videos_quota"],
        }

    # Return a default row (not yet persisted — caller decides quota)
    return {
        "user_uuid": user_uuid,
        "date": date,
        "videos_created": 0,
        "videos_quota": 0,  # placeholder, will be set on first increment
    }


async def increment_usage(
    db,
    user_uuid: str,
    date: str,
    subscription_type: str = "free",
) -> Dict[str, Any]:
    """Atomically increment today's video count.

    Creates a usage row if one doesn't exist yet, using the quota for the
    given subscription_type. If over quota, raises an exception.

    Returns the updated usage dict.
    """
    quota = get_quota(subscription_type)

    # Upsert: try to insert, on conflict increment
    await db.execute(
        """INSERT INTO user_daily_usage (user_uuid, date, videos_created, videos_quota)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(user_uuid, date) DO UPDATE SET
               videos_created = videos_created + 1
        """,
        (user_uuid, date, quota),
    )
    await db.commit()

    # Read back the updated row
    async with db.execute(
        "SELECT user_uuid, date, videos_created, videos_quota "
        "FROM user_daily_usage WHERE user_uuid = ? AND date = ?",
        (user_uuid, date),
    ) as cursor:
        row = await cursor.fetchone()

    if row["videos_created"] > row["videos_quota"]:
        # Revert the increment
        await db.execute(
            "UPDATE user_daily_usage SET videos_created = videos_created - 1 "
            "WHERE user_uuid = ? AND date = ?",
            (user_uuid, date),
        )
        await db.commit()
        raise QuotaExceededError(
            user_uuid=user_uuid,
            date=date,
            used=row["videos_created"],
            quota=row["videos_quota"],
        )

    return {
        "user_uuid": row["user_uuid"],
        "date": row["date"],
        "videos_created": row["videos_created"],
        "videos_quota": row["videos_quota"],
    }


class QuotaExceededError(Exception):
    """Raised when a user has exceeded their daily video quota."""

    def __init__(
        self,
        user_uuid: str,
        date: str,
        used: int,
        quota: int,
    ):
        self.user_uuid = user_uuid
        self.date = date
        self.used = used
        self.quota = quota
        self.message = (
            f"Daily video quota exceeded: {used}/{quota} used on {date}. "
            f"Upgrade your plan for higher limits."
        )
        super().__init__(self.message)
