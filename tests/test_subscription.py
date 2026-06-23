"""Tests for subscription management — TDD for upgrade and subscription endpoints.

Uses TestClient from main:app and sqlite3 for direct DB state checks.
All tests follow RED phase: they will fail until implementation is written.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from config import settings
from db import init_db
from middleware.auth import create_access_token
from middleware.rate_limit import reset_rate_limits

# Initialize DB once at module level (idempotent — CREATE TABLE IF NOT EXISTS)
asyncio.run(init_db())

# Lazy import to avoid circular issues at module scope
from main import app

TEST_USER = {
    "username": "sub_testuser",
    "email": "sub_test@example.com",
    "password": "testpass123",
}

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data and reset rate-limit counters between test runs."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM refresh_tokens")
        conn.execute("DELETE FROM users")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Tables may not exist yet
    finally:
        conn.close()
    reset_rate_limits()


@pytest.fixture
def client():
    """FastAPI TestClient — lifespan triggers init_db."""
    with TestClient(app) as c:
        yield c


# ── Helpers ──────────────────────────────────────────────────────────────


def _register(client) -> dict:
    """Register the test user."""
    resp = client.post("/api/auth/register", json=TEST_USER)
    assert resp.status_code in (201, 409)
    return resp.json()


def _login(client) -> dict:
    """Register + login, return token response."""
    _register(client)
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    return resp.json()


def _auth_header(token: str) -> dict:
    """Return Authorization header dict with Bearer token."""
    return {"Authorization": f"Bearer {token}"}


# ── Tests ────────────────────────────────────────────────────────────────


def test_upgrade_from_free_to_pro(client):
    """Register as free → upgrade to pro → 200 with new plan info."""
    tokens = _login(client)

    resp = client.post(
        "/api/auth/upgrade",
        json={"plan": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_type"] == "pro"
    assert data["status"] == "active"

    # Verify DB users table was updated
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT subscription_type FROM users WHERE username = ?",
        (TEST_USER["username"],),
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "User should exist in DB"
    assert row[0] == "pro", "users.subscription_type should be 'pro'"


def test_upgrade_tier_reflected_in_next_login_jwt(client):
    """Upgrade to pro → login again → JWT contains tier=2."""
    tokens = _login(client)

    # Upgrade to pro
    resp = client.post(
        "/api/auth/upgrade",
        json={"plan": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200

    # Login again to get fresh JWT
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    new_tokens = resp.json()

    # Decode access token and check tier
    payload = jwt.decode(
        new_tokens["access_token"],
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["tier"] == 2, "JWT tier should be 2 for pro user"


def test_get_subscription_free(client):
    """GET /api/auth/subscription for free user returns free plan info."""
    tokens = _login(client)

    resp = client.get(
        "/api/auth/subscription",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_type"] == "free"
    assert "features" in data
    assert isinstance(data["features"], list)
    assert "articles" in data["features"]


def test_get_subscription_after_upgrade(client):
    """Upgrade → GET /api/auth/subscription returns pro plan info."""
    tokens = _login(client)

    # Upgrade to pro
    client.post(
        "/api/auth/upgrade",
        json={"plan": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )

    # Check subscription
    resp = client.get(
        "/api/auth/subscription",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_type"] == "pro"
    assert "voice_clone" in data["features"], "Pro tier should include voice_clone"


def test_free_user_blocked_from_premium_endpoint(client):
    """Free user accessing a premium endpoint → 403."""
    tokens = _login(client)

    resp = client.get(
        "/api/auth/premium-content",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 403
    assert "premium" in resp.json().get("detail", "").lower()


def test_premium_user_can_access_premium_endpoint(client):
    """Pro user accessing a premium endpoint → 200."""
    tokens = _login(client)

    # Upgrade to pro
    client.post(
        "/api/auth/upgrade",
        json={"plan": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )

    # Login again to get fresh JWT with tier=2
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    pro_tokens = resp.json()

    resp = client.get(
        "/api/auth/premium-content",
        headers=_auth_header(pro_tokens["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Premium content unlocked"


def test_upgrade_unauthenticated_returns_401(client):
    """POST /api/auth/upgrade without auth → 401."""
    resp = client.post("/api/auth/upgrade", json={"plan": "pro"})
    assert resp.status_code == 401


def test_upgrade_nonexistent_user_returns_404(client):
    """Authenticated POST /api/auth/upgrade for non-existent user → 404."""
    # Create a valid JWT for a user UUID that doesn't exist in DB
    fake_token = create_access_token(data={
        "sub": "00000000-0000-0000-0000-000000000000",
        "username": "ghost",
        "tier": 1,
    })

    resp = client.post(
        "/api/auth/upgrade",
        json={"plan": "pro"},
        headers=_auth_header(fake_token),
    )
    assert resp.status_code == 404


def test_upgrade_invalid_plan_returns_422(client):
    """POST /api/auth/upgrade with invalid plan value → 422."""
    tokens = _login(client)

    resp = client.post(
        "/api/auth/upgrade",
        json={"plan": "invalid_plan_xyz"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 422
