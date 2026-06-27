"""Tests for admin user management — TDD for admin users CRUD.

Tests use a minimal FastAPI app with only the admin_users router,
bypassing the full app's PG lifespan dependency.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db import init_db, get_db
from middleware.auth import create_access_token

# Initialize DB once at module level
asyncio.run(init_db())

# Import the router directly
from routers.admin_users import router

# Create a minimal test app with just the admin_users router
app = FastAPI()
app.include_router(router, prefix="/api/admin", tags=["admin"])

# ── Test constants ─────────────────────────────────────────────────────

ADMIN_UUID = "00000000-0000-0000-0000-000000000001"
ADMIN_USERNAME = "admin"

USER1_UUID = "00000000-0000-0000-0000-000000000010"
USER1_USERNAME = "alice"
USER1_EMAIL = "alice@example.com"

USER2_UUID = "00000000-0000-0000-0000-000000000011"
USER2_USERNAME = "bob"
USER2_EMAIL = "bob@example.com"

INACTIVE_UUID = "00000000-0000-0000-0000-000000000012"
INACTIVE_USERNAME = "charlie"
INACTIVE_EMAIL = "charlie@example.com"


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


@pytest.fixture
def client():
    """FastAPI TestClient with no lifespan."""
    with TestClient(app) as c:
        yield c


# ── Helpers ────────────────────────────────────────────────────────────


def _insert_test_user(
    uuid: str,
    username: str,
    email: str,
    subscription_type: str = "free",
    is_active: int = 1,
) -> None:
    """Insert a test user directly into the SQLite database."""
    conn = sqlite3.connect("orchestrator.db")
    conn.execute(
        """INSERT INTO users (uuid, username, email, password_hash, subscription_type, is_active)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uuid, username, email, "hash", subscription_type, is_active),
    )
    conn.execute(
        """INSERT OR IGNORE INTO subscriptions (user_uuid, plan_type, status)
           VALUES (?, ?, 'active')""",
        (uuid, subscription_type),
    )
    conn.commit()
    conn.close()


def _insert_usage(uuid: str, date: str, videos_created: int = 5, videos_quota: int = 10) -> None:
    """Insert a usage row for a user."""
    conn = sqlite3.connect("orchestrator.db")
    conn.execute(
        "INSERT OR IGNORE INTO user_daily_usage (user_uuid, date, videos_created, videos_quota) VALUES (?, ?, ?, ?)",
        (uuid, date, videos_created, videos_quota),
    )
    conn.commit()
    conn.close()


def _admin_token() -> str:
    """Create a JWT with admin role."""
    return create_access_token(data={
        "sub": ADMIN_UUID,
        "username": ADMIN_USERNAME,
        "role": "admin",
        "tier": 4,
    })


def _user_token(uuid: str = USER1_UUID, username: str = USER1_USERNAME) -> str:
    """Create a regular user JWT."""
    return create_access_token(data={
        "sub": uuid,
        "username": username,
        "tier": 1,
    })


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Tests: List Users ─────────────────────────────────────────────────


class TestAdminListUsers:
    """GET /api/admin/users — list users with optional filters."""

    def test_list_users_as_admin(self, client):
        """Admin can list all users."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        _insert_test_user(USER2_UUID, USER2_USERNAME, USER2_EMAIL)

        resp = client.get("/api/admin/users", headers=_auth_header(_admin_token()))
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert "total" in data
        assert data["total"] == 2
        usernames = [u["username"] for u in data["users"]]
        assert USER1_USERNAME in usernames
        assert USER2_USERNAME in usernames

    def test_list_users_pagination(self, client):
        """Admin list returns paginated results with page/page_size."""
        for i in range(5):
            uid = f"00000000-0000-0000-0000-00000000002{i}"
            _insert_test_user(uid, f"user{i}", f"user{i}@example.com")

        resp = client.get(
            "/api/admin/users?page=1&page_size=2",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["users"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2

    def test_list_users_filter_by_subscription(self, client):
        """Filter users by subscription_type."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_test_user(USER2_UUID, USER2_USERNAME, USER2_EMAIL, subscription_type="free")

        resp = client.get(
            "/api/admin/users?subscription_type=pro",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["users"][0]["username"] == USER1_USERNAME

    def test_list_users_filter_by_status(self, client):
        """Filter users by is_active status."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, is_active=1)
        _insert_test_user(INACTIVE_UUID, INACTIVE_USERNAME, INACTIVE_EMAIL, is_active=0)

        resp = client.get(
            "/api/admin/users?is_active=false",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["users"][0]["username"] == INACTIVE_USERNAME

    def test_list_users_search(self, client):
        """Search users by username or email."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        _insert_test_user(USER2_UUID, USER2_USERNAME, USER2_EMAIL)

        resp = client.get(
            "/api/admin/users?search=alice",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["users"][0]["email"] == USER1_EMAIL

    def test_list_users_requires_admin(self, client):
        """Non-admin user gets 403."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        resp = client.get("/api/admin/users", headers=_auth_header(_user_token()))
        assert resp.status_code == 403

    def test_list_users_unauthenticated(self, client):
        """No auth token returns 401."""
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401

    def test_list_users_empty(self, client):
        """List with no users returns empty list."""
        resp = client.get("/api/admin/users", headers=_auth_header(_admin_token()))
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == []
        assert data["total"] == 0


# ── Tests: Get User Detail ────────────────────────────────────────────


class TestAdminGetUser:
    """GET /api/admin/users/{uuid} — get user detail."""

    def test_get_user_detail(self, client):
        """Admin can get user detail with subscription and usage info."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, subscription_type="pro")
        _insert_usage(USER1_UUID, "2026-06-27")

        resp = client.get(
            f"/api/admin/users/{USER1_UUID}",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["uuid"] == USER1_UUID
        assert data["username"] == USER1_USERNAME
        assert data["subscription_type"] == "pro"
        assert data["is_active"] is True
        assert "subscription" in data
        assert data["subscription"]["plan_type"] == "pro"
        assert "usage" in data
        assert len(data["usage"]) >= 1

    def test_get_user_not_found(self, client):
        """Get non-existent user returns 404."""
        resp = client.get(
            f"/api/admin/users/{USER1_UUID}",
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 404

    def test_get_user_requires_admin(self, client):
        """Non-admin gets 403."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        resp = client.get(
            f"/api/admin/users/{USER1_UUID}",
            headers=_auth_header(_user_token()),
        )
        assert resp.status_code == 403


# ── Tests: Toggle User Status ─────────────────────────────────────────


class TestAdminToggleUserStatus:
    """PUT /api/admin/users/{uuid}/status — activate/deactivate user."""

    def test_deactivate_user(self, client):
        """Admin can deactivate a user."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL, is_active=1)

        resp = client.put(
            f"/api/admin/users/{USER1_UUID}/status",
            json={"is_active": False},
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        conn = sqlite3.connect("orchestrator.db")
        cursor = conn.execute("SELECT is_active FROM users WHERE uuid = ?", (USER1_UUID,))
        row = cursor.fetchone()
        conn.close()
        assert row[0] == 0

    def test_activate_user(self, client):
        """Admin can reactivate a deactivated user."""
        _insert_test_user(INACTIVE_UUID, INACTIVE_USERNAME, INACTIVE_EMAIL, is_active=0)

        resp = client.put(
            f"/api/admin/users/{INACTIVE_UUID}/status",
            json={"is_active": True},
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        conn = sqlite3.connect("orchestrator.db")
        cursor = conn.execute("SELECT is_active FROM users WHERE uuid = ?", (INACTIVE_UUID,))
        row = cursor.fetchone()
        conn.close()
        assert row[0] == 1

    def test_toggle_user_not_found(self, client):
        """Toggle non-existent user returns 404."""
        resp = client.put(
            f"/api/admin/users/{USER1_UUID}/status",
            json={"is_active": False},
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 404

    def test_toggle_user_requires_admin(self, client):
        """Non-admin gets 403."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        resp = client.put(
            f"/api/admin/users/{USER1_UUID}/status",
            json={"is_active": False},
            headers=_auth_header(_user_token()),
        )
        assert resp.status_code == 403

    def test_toggle_user_invalid_body(self, client):
        """Missing is_active field returns 422."""
        _insert_test_user(USER1_UUID, USER1_USERNAME, USER1_EMAIL)
        resp = client.put(
            f"/api/admin/users/{USER1_UUID}/status",
            json={},
            headers=_auth_header(_admin_token()),
        )
        assert resp.status_code == 422
