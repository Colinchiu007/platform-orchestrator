"""Subscription lifecycle daemon — Membership Phase 2.

Scans for expired subscriptions on startup, marks them as expired,
and downgrades affected users to 'free' plan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

from db import DB_PATH

logger = logging.getLogger(__name__)


async def check_expired_subscriptions(db: aiosqlite.Connection) -> int:
    """Find all active subscriptions past their end_date and expire them.

    For each expired subscription:
      1. Set subscriptions.status = 'expired'
      2. Set users.subscription_type = 'free'

    Returns the number of subscriptions expired.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Find expired active subscriptions
    cursor = await db.execute(
        """SELECT s.user_uuid, s.plan_type, s.end_date
           FROM subscriptions s
           WHERE s.status = 'active'
             AND s.end_date IS NOT NULL
             AND s.end_date < ?""",
        (now,),
    )
    rows = await cursor.fetchall()

    if not rows:
        return 0

    user_uuids = [row["user_uuid"] for row in rows]

    # Update subscriptions to expired
    await db.execute(
        """UPDATE subscriptions
           SET status = 'expired'
           WHERE status = 'active'
             AND end_date IS NOT NULL
             AND end_date < ?""",
        (now,),
    )

    # Downgrade users to free
    placeholders = ",".join("?" for _ in user_uuids)
    await db.execute(
        f"""UPDATE users
            SET subscription_type = 'free', updated_at = datetime('now')
            WHERE uuid IN ({placeholders})""",
        user_uuids,
    )

    await db.commit()

    for row in rows:
        logger.info(
            "Expired subscription: user=%s plan=%s end_date=%s",
            row["user_uuid"], row["plan_type"], row["end_date"],
        )

    return len(rows)


async def daily_maintenance() -> None:
    """Open a fresh DB connection and run subscription expiry check.

    Intended to be called on application startup (lifespan hook).
    Opens its own connection so it does not interfere with request-scoped
    dependencies.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    try:
        expired_count = await check_expired_subscriptions(db)
        if expired_count:
            logger.info("Subscription maintenance: expired %d subscription(s)", expired_count)
        else:
            logger.info("Subscription maintenance: no expired subscriptions found")
    except Exception:
        logger.exception("Subscription maintenance failed")
    finally:
        await db.close()
