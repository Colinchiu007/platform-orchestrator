"""Tests for payment/billing routes — TDD for checkout, webhook, history.

Uses TestClient from main:app and sqlite3 for direct DB state checks.
Mock payment provider is injected via router config (no real APIs needed).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from db import init_db
from middleware.rate_limit import reset_rate_limits

# Initialize DB once at module level (idempotent — CREATE TABLE IF NOT EXISTS)
asyncio.run(init_db())

# Lazy import to avoid circular issues at module scope
from main import app  # noqa: E402

TEST_USER = {
    "username": "pay_testuser",
    "email": "pay_test@example.com",
    "password": "testpass123",
}

PLAN_PRICES = {
    "basic": 999,    # $9.99
    "pro": 2999,     # $29.99
    "enterprise": 9999,  # $99.99
}


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data and reset rate-limit counters between test runs."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM payments")
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


def _webhook_headers(body: dict) -> dict:
    """Compute HMAC-SHA256 signature and return headers including it.

    Uses the same serialization as TestClient.json= so the server-side
    request.body() produces byte-identical output.
    """
    import hashlib, hmac, json

    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode()
    sig = hmac.new(b"dev-webhook-secret", raw, hashlib.sha256).hexdigest()
    return {"X-Webhook-Signature": sig}


# ── Tests ────────────────────────────────────────────────────────────────


def test_create_checkout_success(client):
    """Authenticated user creates a checkout session → 200 with checkout URL."""
    tokens = _login(client)

    resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_type"] == "pro"
    assert data["amount_cents"] == PLAN_PRICES["pro"]
    assert data["currency"] == "usd"
    assert data["status"] == "pending"
    assert data["checkout_id"] is not None
    assert data["checkout_url"] is not None
    assert "checkout" in data["checkout_url"].lower()

    # Verify payment row exists in DB
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT checkout_id, plan_type, amount_cents, status FROM payments WHERE checkout_id = ?",
        (data["checkout_id"],),
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "No payments row found after checkout"
    assert row[1] == "pro"
    assert row[2] == PLAN_PRICES["pro"]
    assert row[3] == "pending"


def test_create_checkout_unauthenticated(client):
    """POST /api/payment/create-checkout without auth → 401."""
    resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "pro"},
    )
    assert resp.status_code == 401


def test_create_checkout_invalid_plan(client):
    """POST /api/payment/create-checkout with invalid plan → 422."""
    tokens = _login(client)

    resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "nonexistent_plan"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 422


def test_webhook_success(client):
    """POST webhook with valid checkout_id → 200, payment marked completed,
    and user subscription upgraded."""
    tokens = _login(client)

    # Create checkout first
    checkout_resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert checkout_resp.status_code == 200
    checkout_id = checkout_resp.json()["checkout_id"]

    # Simulate payment completion via webhook
    body = {"checkout_id": checkout_id, "status": "completed"}
    resp = client.post(
        "/api/payment/webhook",
        json=body,
        headers=_webhook_headers(body),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"

    # Verify DB row is now completed
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT status FROM payments WHERE checkout_id = ?",
        (checkout_id,),
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "completed"

    # Verify user's subscription was upgraded to pro
    resp = client.get(
        "/api/auth/subscription",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["plan_type"] == "pro"


def test_webhook_invalid_checkout_id(client):
    """POST webhook with non-existent checkout_id → 404."""
    body = {"checkout_id": "non-existent-id", "status": "completed"}
    resp = client.post(
        "/api/payment/webhook",
        json=body,
        headers=_webhook_headers(body),
    )
    assert resp.status_code == 404


def test_webhook_failed_status(client):
    """POST webhook with status=failed → payment marked failed, subscription unchanged."""
    tokens = _login(client)

    # Create checkout
    checkout_resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "pro"},
        headers=_auth_header(tokens["access_token"]),
    )
    checkout_id = checkout_resp.json()["checkout_id"]

    # Simulate payment failure
    body = {"checkout_id": checkout_id, "status": "failed"}
    resp = client.post(
        "/api/payment/webhook",
        json=body,
        headers=_webhook_headers(body),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"

    # Verify DB
    conn = sqlite3.connect("orchestrator.db")
    cursor = conn.execute(
        "SELECT status FROM payments WHERE checkout_id = ?",
        (checkout_id,),
    )
    row = cursor.fetchone()
    conn.close()
    assert row[0] == "failed"

    # User should still be on free plan
    sub_resp = client.get(
        "/api/auth/subscription",
        headers=_auth_header(tokens["access_token"]),
    )
    assert sub_resp.json()["plan_type"] == "free"


def test_payment_history(client):
    """GET /api/payment/history returns payment records for current user."""
    tokens = _login(client)

    # Create two checkouts with different plans
    for plan in ["basic", "pro"]:
        client.post(
            "/api/payment/create-checkout",
            json={"plan_type": plan},
            headers=_auth_header(tokens["access_token"]),
        )

    resp = client.get(
        "/api/payment/history",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "payments" in data
    assert len(data["payments"]) == 2

    plans_found = {p["plan_type"] for p in data["payments"]}
    assert "basic" in plans_found
    assert "pro" in plans_found

    # All should be pending
    for p in data["payments"]:
        assert p["status"] == "pending"
        assert p["amount_cents"] == PLAN_PRICES[p["plan_type"]]
        assert p["currency"] == "usd"


def test_payment_history_unauthenticated(client):
    """GET /api/payment/history without auth → 401."""
    resp = client.get("/api/payment/history")
    assert resp.status_code == 401


def test_full_payment_flow(client):
    """Complete payment flow: checkout → webhook complete → history shows completed."""
    tokens = _login(client)

    # Step 1: Create checkout
    checkout_resp = client.post(
        "/api/payment/create-checkout",
        json={"plan_type": "enterprise"},
        headers=_auth_header(tokens["access_token"]),
    )
    assert checkout_resp.status_code == 200
    checkout_id = checkout_resp.json()["checkout_id"]

    # Step 2: Webhook marks completed
    body = {"checkout_id": checkout_id, "status": "completed"}
    client.post(
        "/api/payment/webhook",
        json=body,
        headers=_webhook_headers(body),
    )

    # Step 3: History should show it
    resp = client.get(
        "/api/payment/history",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    payments = resp.json()["payments"]
    assert len(payments) == 1
    assert payments[0]["checkout_id"] == checkout_id
    assert payments[0]["status"] == "completed"
    assert payments[0]["plan_type"] == "enterprise"
    assert payments[0]["amount_cents"] == PLAN_PRICES["enterprise"]


def test_webhook_invalid_signature(client):
    """POST webhook with wrong signature → 401."""
    resp = client.post(
        "/api/payment/webhook",
        json={"checkout_id": "any", "status": "completed"},
        headers={"X-Webhook-Signature": "invalid-sig"},
    )
    assert resp.status_code == 401
    assert "Invalid webhook signature" in resp.json()["detail"]


def test_payment_history_empty_for_new_user(client):
    """Newly registered user has no payment history."""
    tokens = _login(client)

    resp = client.get(
        "/api/payment/history",
        headers=_auth_header(tokens["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["payments"] == []
