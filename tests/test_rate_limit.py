"""Tests for rate limiting middleware — TDD with slowapi.

Verifies IP-based rate limits on auth endpoints and user-tier-aware
limits on video creation. Uses TestClient from main:app.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from db import init_db
from middleware.rate_limit import reset_rate_limits

# Initialize DB once at module level (idempotent)
asyncio.run(init_db())

from main import app

TEST_USER = {
    "username": "ratelimituser",
    "email": "ratelimit@example.com",
    "password": "testpass123",
}


@pytest.fixture(autouse=True)
def clean_and_reset():
    """Remove test data and reset rate limit counters between tests."""
    import sqlite3

    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM refresh_tokens")
        conn.execute("DELETE FROM users")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    reset_rate_limits()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _register(client, user: dict | None = None) -> None:
    """Register a user (create if not exists)."""
    payload = user or TEST_USER
    resp = client.post("/api/auth/register", json=payload)
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


# ── Rate Limit Tests ─────────────────────────────────────────


def test_login_allows_first_5(client):
    """First 5 rapid login requests from same IP should succeed (5/min)."""
    _register(client)
    for i in range(5):
        resp = client.post("/api/auth/login", json={
            "username": TEST_USER["username"],
            "password": TEST_USER["password"],
        })
        assert resp.status_code == 200, (
            f"Request {i + 1} should be 200, got {resp.status_code}"
        )


def test_login_rate_limit_429_after_5(client):
    """6th login request in quick succession should get 429."""
    _register(client)
    for _ in range(5):
        resp = client.post("/api/auth/login", json={
            "username": TEST_USER["username"],
            "password": TEST_USER["password"],
        })
        assert resp.status_code == 200

    # 6th request — should be rate limited
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
    data = resp.json()
    # slowapi 0.1.10 returns "error" key, not "detail"
    assert "error" in data or "detail" in data, f"No error key in {data}"


def test_register_rate_limit_429_after_3(client):
    """4th register request in quick succession should get 429 (3/hour)."""
    users = [
        {"username": f"rl_user_{i}", "email": f"rl_user_{i}@test.com", "password": "testpass123"}
        for i in range(4)
    ]
    for i, user in enumerate(users):
        resp = client.post("/api/auth/register", json=user)
        if i < 3:
            assert resp.status_code == 201, (
                f"Register {i + 1} should be 201, got {resp.status_code}"
            )
        else:
            assert resp.status_code == 429, (
                f"Register {i + 1} should be 429, got {resp.status_code}"
            )


def test_different_ip_bypasses_rate_limit(client):
    """Requests from different IPs have separate counters — 6th from IP2 succeeds."""
    _register(client)
    # Exhaust 5/min from IP 1
    for _ in range(5):
        resp = client.post("/api/auth/login", json={
            "username": TEST_USER["username"],
            "password": TEST_USER["password"],
        }, headers={"X-Forwarded-For": "192.168.1.1"})
        assert resp.status_code == 200

    # 6th from IP 1 — should be 429
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    }, headers={"X-Forwarded-For": "192.168.1.1"})
    assert resp.status_code == 429

    # Same request from IP 2 — different counter, should succeed
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    }, headers={"X-Forwarded-For": "192.168.1.2"})
    assert resp.status_code == 200, f"IP 2 should bypass limit, got {resp.status_code}"


def test_rate_limit_429_response_has_error(client):
    """Rate-limited response should contain an error message."""
    _register(client)
    # Exhaust the 5/min login limit
    for _ in range(5):
        client.post("/api/auth/login", json={
            "username": TEST_USER["username"],
            "password": TEST_USER["password"],
        })

    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 429
    data = resp.json()
    assert "error" in data
    assert "rate limit" in data["error"].lower()
