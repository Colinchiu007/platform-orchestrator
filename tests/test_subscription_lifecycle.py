"""Tests for subscription lifecycle daemon — Membership Phase 2.

Tests use SQLite directly (no TestClient) to verify:
- Expired subscriptions are detected and expired
- Affected users are downgraded to 'free'
- Future / NULL end_date subscriptions remain active
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from db import init_db

# Initialize DB once at module level
asyncio.run(init_db())

# ── Test constants ─────────────────────────────────────────────────────

USER1_UUID = "00000000-0000-0000-0000-000000000010"
USER1_USERNAME = "alice"
USER1_EMAIL = "alice@example.com"

USER2_UUID = "00000000-0000-0000-0000-000000000011"
USER2_USERNAME = "bob"
USER2_EMAIL = "bob@example.com"

USER3_UUID = "00000000-0000-0000-0000-000000000012"
USER3_USERNAME = "charlie"
USER3_EMAIL = "charlie@example.com"


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data between test runs."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM user_daily_usage")
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM refresh_tokens")
        conn.execute("DELETE FROM users")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


# ── Helpers ────────────────────────────────────────────────────────────


def _insert_user(
    uuid: str,
    username: str,
    email: str,
    subscription_type: str = "free",
    is_active: int = 1,
) -> None:
    """Insert a test user."""
    conn = sqlite3.connect("orchestrator.db")
    conn.execute(
        """INSERT INTO users (uuid, username, email, password_hash, subscription_type, is_active)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uuid, username, email, "hash", subscription_type, is_active),
    )
    conn.commit()
    conn.close()


def _insert_subscription(
    user_uuid: str,
    plan_type: str = "pro",
    status: str = "active",
    end_date: str | None = None,
) -> None:
    """Insert a subscription row."""
    conn = sqlite3.connect("orchestrator.db")
    conn.execute(
        """INSERT OR IGNORE INTO subscriptions (user_uuid, plan_type, status, start_date, end_date)
           VALUES (?, ?, ?, datetime('now'), ?)""",
        (user_uuid, plan_type, status, end_date),
    )
    conn.commit()
    conn.close()


def _get_user_subscription_type(uuid: str) -> str | None:
    """Read a user's current subscription_type from the database."""
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT subscription_type FROM users WHERE uuid = ?", (uuid,),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def _get_subscription_status(uuid: str) -> str | None:
    """Read a subscription's current status."""
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT status FROM subscriptions WHERE user_uuid = ?", (uuid,),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


# ── Tests: check_expired_subscriptions ─────────────────────────────────


class TestCheckExpiredSubscriptions:
    """Subscription expiry detection and user downgrade logic."""

    @pytest.mark.asyncio
    async def test_expires_past_end_date(self):
        """Subscription with end_date in the past gets expired and user downgraded."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_subscription(USER1_UUID, plan_type="pro", end_date="2020-01-01T00:00:00")

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 1
        assert _get_subscription_status(USER1_UUID) == "expired"
        assert _get_user_subscription_type(USER1_UUID) == "free"

    @pytest.mark.asyncio
    async def test_keeps_future_end_date(self):
        """Subscription with end_date in the future stays active."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_subscription(USER1_UUID, plan_type="pro", end_date="2099-12-31T23:59:59")

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 0
        assert _get_subscription_status(USER1_UUID) == "active"
        assert _get_user_subscription_type(USER1_UUID) == "pro"

    @pytest.mark.asyncio
    async def test_keeps_null_end_date(self):
        """Subscription with NULL end_date (lifetime/perpetual) stays active."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_subscription(USER1_UUID, plan_type="pro", end_date=None)

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 0
        assert _get_subscription_status(USER1_UUID) == "active"
        assert _get_user_subscription_type(USER1_UUID) == "pro"

    @pytest.mark.asyncio
    async def test_keeps_already_expired(self):
        """Already-expired subscriptions are not touched again."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="free")
        _insert_subscription(USER1_UUID, plan_type="basic", status="expired", end_date="2020-01-01T00:00:00")

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 0
        assert _get_subscription_status(USER1_UUID) == "expired"

    @pytest.mark.asyncio
    async def test_expires_only_past_dates(self):
        """Only subscriptions past end_date are expired; future ones remain."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_subscription(USER1_UUID, plan_type="pro", end_date="2020-01-01T00:00:00")

        _insert_user(USER2_UUID, USER2_USERNAME, USER2_EMAIL, subscription_type="basic")
        _insert_subscription(USER2_UUID, plan_type="basic", end_date="2099-12-31T23:59:59")

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 1
        # USER1 (past) expired
        assert _get_subscription_status(USER1_UUID) == "expired"
        assert _get_user_subscription_type(USER1_UUID) == "free"
        # USER2 (future) still active
        assert _get_subscription_status(USER2_UUID) == "active"
        assert _get_user_subscription_type(USER2_UUID) == "basic"

    @pytest.mark.asyncio
    async def test_no_expired_returns_zero(self):
        """No expired subscriptions returns 0."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="free")
        _insert_subscription(USER1_UUID, plan_type="free", end_date="2099-12-31T23:59:59")

        from services.subscription_lifecycle import check_expired_subscriptions
        from db import DB_PATH
        import aiosqlite

        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            count = await check_expired_subscriptions(db)
        finally:
            await db.close()

        assert count == 0


class TestDailyMaintenance:
    """daily_maintenance() integration smoke test."""

    @pytest.mark.asyncio
    async def test_daily_maintenance_expires(self):
        """daily_maintenance runs without error and expires past subscriptions."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_subscription(USER1_UUID, plan_type="pro", end_date="2020-01-01T00:00:00")

        from services.subscription_lifecycle import daily_maintenance

        await daily_maintenance()

        assert _get_subscription_status(USER1_UUID) == "expired"
        assert _get_user_subscription_type(USER1_UUID) == "free"

    @pytest.mark.asyncio
    async def test_daily_maintenance_noop(self):
        """daily_maintenance with no expired subs runs cleanly."""
        _insert_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="free")
        _insert_subscription(USER1_UUID, plan_type="free", end_date="2099-12-31T23:59:59")

        from services.subscription_lifecycle import daily_maintenance

        await daily_maintenance()
        # No crash = success
        assert True
