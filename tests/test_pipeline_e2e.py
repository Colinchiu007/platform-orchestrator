"""Pipeline E2E tests — health, auth, admin CRUD, user provider ops, feature gates, usage.

Follows the same patterns as test_e2e_pipeline.py: TestClient, class-based
organization, unique usernames per test to avoid DB conflicts.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from middleware.auth import create_access_token
from middleware.rate_limit import reset_rate_limits
from services.provider_router import get_router


# ── Helpers ────────────────────────────────────────────────────────────────────


def _register_and_login(client, username: str, password: str = "testpass123") -> dict:
    """Register + login, return auth header dict."""
    resp = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": password,
    })
    assert resp.status_code in (201, 409)

    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _admin_auth_header() -> dict:
    """Create an admin JWT and return auth header dict."""
    token = create_access_token({"sub": "admin-uuid", "username": "admin", "role": "admin", "tier": 3})
    return {"Authorization": f"Bearer {token}"}


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data between runs to guarantee isolation."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM provider_configs")
        conn.execute("DELETE FROM user_api_keys")
        conn.commit()  # Critical: without commit the DELETE is rolled back on close
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    # Also clean the auth DB (test_auth.db) if it exists
    try:
        conn2 = sqlite3.connect("test_auth.db")
        conn2.execute("DELETE FROM users")
        conn2.execute("DELETE FROM refresh_tokens")
        conn2.execute("DELETE FROM subscriptions")
        conn2.commit()
        conn2.close()
    except (sqlite3.OperationalError, FileNotFoundError):
        pass
    # Also clean test.db (main SQLAlchemy DB)
    try:
        conn3 = sqlite3.connect("test.db")
        conn3.execute("DELETE FROM users")
        conn3.execute("DELETE FROM refresh_tokens")
        conn3.execute("DELETE FROM subscriptions")
        conn3.commit()
        conn3.close()
    except (sqlite3.OperationalError, FileNotFoundError):
        pass
    reset_rate_limits()


@pytest.fixture(scope="session")
def client():
    """Provide a TestClient instance (session-scoped to avoid repeated lifespan init)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _init_provider_router():
    """Initialize ProviderRouter tables once per session.

    This runs before the app's lifespan so provider tables exist
    before any test needs them.
    """
    import asyncio

    router = get_router()
    asyncio.run(router.init_db())


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestBasicEndpoints:
    """Basic service health and feature endpoints."""

    def test_health_check(self, client):
        """GET /health returns 200 with status ok."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_feature_gates(self, client):
        """GET /api/features returns 200 with features dict.

        The feature_gates.yaml file does not exist in the test
        environment, so we expect an empty dict.
        """
        resp = client.get("/api/features")
        assert resp.status_code == 200
        data = resp.json()
        assert "features" in data
        # File not found => empty gates
        assert isinstance(data["features"], dict)

    def test_health_all_endpoint(self, client):
        """GET /api/health/all returns aggregated service status."""
        resp = client.get("/api/health/all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "total" in data
        assert "healthy" in data
        assert "services" in data
        assert isinstance(data["services"], list)
        assert len(data["services"]) == data["total"]


class TestAuth:
    """Authentication — register, login, JWT issuance."""

    def test_register_user(self, client):
        """POST /api/auth/register creates a user and returns 201."""
        resp = client.post("/api/auth/register", json={
            "username": "fresh_user",
            "email": "fresh_user@example.com",
            "password": "securepass123",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "fresh_user"
        assert data["subscription_type"] == "free"

    def test_login_returns_jwt(self, client):
        """POST /api/auth/login returns 200 with access_token and refresh_token."""
        # Register first
        client.post("/api/auth/register", json={
            "username": "jwt_user",
            "email": "jwt_user@example.com",
            "password": "testpass123",
        })
        # Login
        resp = client.post("/api/auth/login", json={
            "username": "jwt_user",
            "password": "testpass123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"


class TestProviderAdminCRUD:
    """Admin CRUD for provider configurations."""

    @staticmethod
    def _provider_payload(suffix: str = "") -> dict:
        """Return a provider payload dict with a unique name per test."""
        return {
            "name": f"test-provider{suffix}",
            "provider_type": "llm",
            "display_name": f"Test LLM{suffix}",
            "base_url": f"https://api.test-provider{suffix}.com/v1",
            "api_key": f"sk-test-key-12345{suffix}",
            "models": ["gpt-4", "gpt-3.5"],
            "enabled": True,
            "min_tier": 1,
        }

    def test_create_provider(self, client):
        """POST /api/admin/providers creates a provider and returns 201."""
        auth = _admin_auth_header()
        resp = client.post("/api/admin/providers", json=self._provider_payload("_cr"), headers=auth)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-provider_cr"
        assert data["provider_type"] == "llm"

    def test_list_providers(self, client):
        """GET /api/admin/providers returns a list of providers."""
        auth = _admin_auth_header()

        # Create one first
        client.post("/api/admin/providers", json=self._provider_payload("_list"), headers=auth)

        # List
        resp = client.get("/api/admin/providers", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = [p["name"] for p in data]
        assert "test-provider_list" in names

    def test_update_provider(self, client):
        """PUT /api/admin/providers/{name} updates a provider."""
        auth = _admin_auth_header()

        client.post("/api/admin/providers", json=self._provider_payload("_up"), headers=auth)

        resp = client.put("/api/admin/providers/test-provider_up", json={
            "display_name": "Updated LLM",
            "min_tier": 2,
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "Updated LLM"
        assert data["min_tier"] == 2

    def test_delete_provider(self, client):
        """DELETE /api/admin/providers/{name} deletes a provider and returns 204."""
        auth = _admin_auth_header()

        client.post("/api/admin/providers", json=self._provider_payload("_del"), headers=auth)

        resp = client.delete("/api/admin/providers/test-provider_del", headers=auth)
        assert resp.status_code == 204

    def test_full_crud_cycle(self, client):
        """Full admin CRUD cycle: create, list, update, get, delete."""
        auth = _admin_auth_header()

        # 1. Create
        resp = client.post("/api/admin/providers", json=self._provider_payload("_full"), headers=auth)
        assert resp.status_code == 201

        # 2. List (should include the new provider)
        resp = client.get("/api/admin/providers", headers=auth)
        assert resp.status_code == 200
        items = resp.json()
        assert any(p["name"] == "test-provider_full" for p in items)

        # 3. Update
        resp = client.put("/api/admin/providers/test-provider_full", json={
            "display_name": "CRUD Updated",
            "enabled": False,
        }, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "CRUD Updated"

        # 4. Delete
        resp = client.delete("/api/admin/providers/test-provider_full", headers=auth)
        assert resp.status_code == 204

        # 5. Verify deletion
        resp = client.get("/api/admin/providers", headers=auth)
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "test-provider_full" not in names

    def test_admin_rejects_non_admin(self, client):
        """Non-admin users get 403 on admin provider endpoints."""
        user_auth = _register_and_login(client, "regular_joe")

        resp = client.post("/api/admin/providers", json=self._provider_payload("_nope"), headers=user_auth)
        assert resp.status_code == 403

        resp = client.get("/api/admin/providers", headers=user_auth)
        assert resp.status_code == 403

        resp = client.delete("/api/admin/providers/test-provider", headers=user_auth)
        assert resp.status_code == 403


class TestUserProviderOperations:
    """User-facing provider API key management."""

    @staticmethod
    def _provider_payload(suffix: str = "") -> dict:
        """Return a provider payload dict with a unique name per test."""
        return {
            "name": f"user-test-provider{suffix}",
            "provider_type": "llm",
            "display_name": f"User Test LLM{suffix}",
            "base_url": f"https://api.user-test{suffix}.com/v1",
            "api_key": "sk-admin-key-98765",
            "models": ["gpt-4"],
            "enabled": True,
            "min_tier": 1,
        }

    def test_list_available_providers(self, client):
        """GET /api/user/providers lists available providers for user's tier."""
        # Admin creates a provider first
        admin_auth = _admin_auth_header()
        client.post("/api/admin/providers", json=self._provider_payload("_list"), headers=admin_auth)

        # User views available providers
        user_auth = _register_and_login(client, "provider_user_list")
        resp = client.get("/api/user/providers", headers=user_auth)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = [p["name"] for p in data]
        assert "user-test-provider_list" in names

    def test_set_and_view_provider_key(self, client):
        """User can set own API key and view masked version."""
        admin_auth = _admin_auth_header()
        client.post("/api/admin/providers", json=self._provider_payload("_key"), headers=admin_auth)

        user_auth = _register_and_login(client, "provider_key_user")
        user_key = "sk-user-private-key-abc123"

        # Set user key
        resp = client.put("/api/user/providers/user-test-provider_key/key", json={
            "api_key": user_key,
        }, headers=user_auth)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # View provider (key should be masked)
        resp = client.get("/api/user/providers/user-test-provider_key", headers=user_auth)
        assert resp.status_code == 200
        data = resp.json()
        # For non-admin view, key should be masked
        assert "api_key" in data
        masked = data["api_key"]
        assert masked.startswith("sk-u")
        assert masked.endswith("c123")
        assert "***" in masked or "*" in masked

    def test_delete_user_key(self, client):
        """User can delete own API key override."""
        admin_auth = _admin_auth_header()
        client.post("/api/admin/providers", json=self._provider_payload("_del"), headers=admin_auth)

        user_auth = _register_and_login(client, "provider_del_key")
        client.put("/api/user/providers/user-test-provider_del/key", json={
            "api_key": "sk-temp-key",
        }, headers=user_auth)

        # Delete key
        resp = client.delete("/api/user/providers/user-test-provider_del/key", headers=user_auth)
        assert resp.status_code == 204


class TestUsageTracking:
    """Daily usage tracking for authenticated users."""

    def test_usage_requires_auth(self, client):
        """GET /api/user/usage returns 401 without auth."""
        resp = client.get("/api/user/usage")
        assert resp.status_code == 401

    def test_usage_returns_daily_info(self, client):
        """GET /api/user/usage returns usage info for authenticated user."""
        user_auth = _register_and_login(client, "usage_test_user")

        resp = client.get("/api/user/usage", headers=user_auth)
        assert resp.status_code == 200
        data = resp.json()

        # Expected keys from the usage endpoint
        assert "videos_used" in data
        assert "videos_quota" in data
        assert "reset_time" in data
        assert "plan_type" in data
        assert "date" in data

        # Free plan: 3 videos per day
        assert data["videos_used"] == 0
        assert data["videos_quota"] == 3
        assert data["plan_type"] == "free"
