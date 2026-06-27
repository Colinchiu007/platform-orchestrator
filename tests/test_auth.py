"""Tests for authentication routes — TDD for refresh token persistence and revocation.

Uses TestClient from main:app and sqlite3 for direct DB state checks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from config import settings
from middleware.rate_limit import reset_rate_limits

# DB init is handled by conftest.py session fixture — no module-level asyncio.run()
# Rate-limit bypass is handled by conftest.py session fixture

# Register test user once, reuse across tests
TEST_USER = {
    "username": "testuser",
    "email": "test@example.com",
    "password": "testpass123",
}


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data and reset rate-limit counters between test runs."""
    conn = sqlite3.connect("test_auth.db")
    try:
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


# Lazy import to avoid circular issues at module scope
from main import app


def _register(client):
    """Register the test user."""
    resp = client.post("/api/auth/register", json=TEST_USER)
    # 409 is fine if user was already registered in a previous failed test
    assert resp.status_code in (201, 409)


def _login(client) -> dict:
    """Register + login, return token response."""
    _register(client)
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    return resp.json()


def _get_jti(token: str) -> str:
    """Extract jti claim from a JWT."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])["jti"]


# ── Tests ────────────────────────────────────────────────────────────────────


def test_login_creates_refresh_row(client):
    """After login, a row in refresh_tokens must exist with matching jti."""
    tokens = _login(client)
    jti = _get_jti(tokens["refresh_token"])

    conn = sqlite3.connect("test_auth.db")
    cursor = conn.execute(
        "SELECT token_jti, user_uuid, revoked FROM refresh_tokens WHERE token_jti = ?",
        (jti,),
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None, "No refresh_tokens row found after login"
    assert row[0] == jti
    assert row[2] == 0, "Token should not be revoked on creation"


def test_refresh_valid_token(client):
    """Login → refresh → new access token (200)."""
    tokens = _login(client)

    resp = client.post("/api/auth/refresh", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # The new access token should be different from the original
    assert data["access_token"] != tokens["access_token"]


def test_logout_revokes(client):
    """Login → logout → refresh_tokens row has revoked=1."""
    tokens = _login(client)
    jti = _get_jti(tokens["refresh_token"])

    resp = client.post("/api/auth/logout", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert resp.status_code == 200

    conn = sqlite3.connect("test_auth.db")
    cursor = conn.execute(
        "SELECT revoked FROM refresh_tokens WHERE token_jti = ?",
        (jti,),
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 1, "Token should be marked revoked after logout"


def test_refresh_revoked_token(client):
    """Login → logout → refresh → 401."""
    tokens = _login(client)

    # Logout first
    client.post("/api/auth/logout", json={
        "refresh_token": tokens["refresh_token"],
    })

    # Try refreshing with revoked token
    resp = client.post("/api/auth/refresh", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert resp.status_code == 401


def test_refresh_expired_token(client):
    """Using a token with past exp → 401."""
    expired_token = jwt.encode(
        {
            "sub": "00000000-0000-0000-0000-000000000000",
            "type": "refresh",
            "jti": "expired-jti",
            "exp": datetime.utcnow() - timedelta(hours=1),
            "iat": datetime.utcnow() - timedelta(days=2),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )

    resp = client.post("/api/auth/refresh", json={
        "refresh_token": expired_token,
    })
    assert resp.status_code == 401


def test_refresh_invalid_token(client):
    """Nonsense token → 401."""
    resp = client.post("/api/auth/refresh", json={
        "refresh_token": "this.is.not.a.valid.token",
    })
    assert resp.status_code == 401
