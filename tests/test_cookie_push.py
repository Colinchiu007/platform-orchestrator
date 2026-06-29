"""Tests for cookie push endpoint — POST /api/jobs/cookies/{platform}.

Uses X-API-Key auth to bypass PG dependency.
Verifies cookie storage for create/update paths.
"""

from __future__ import annotations

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from db import init_db
from middleware.rate_limit import reset_rate_limits

# Set API key before importing main
os.environ["PO_API_KEY"] = "test-cookie-push-key"

from main import app

TEST_API_KEY = "test-cookie-push-key"

SAMPLE_COOKIES = [
    {"name": "sid_tt", "value": "test_sid_value", "domain": ".douyin.com", "path": "/"},
    {"name": "sessionid", "value": "test_session_value", "domain": ".douyin.com", "path": "/"},
    {"name": "csrf_session_id", "value": "test_csrf", "domain": ".douyin.com", "path": "/"},
]


@pytest.fixture(autouse=True)
def clean_providers():
    """Clean provider_configs between tests to guarantee isolation."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM provider_configs WHERE name IN ('douyin', 'bilibili')")
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


@pytest.fixture
def api_auth() -> dict:
    """Return X-API-Key auth header."""
    return {"X-API-Key": TEST_API_KEY}


class TestCookiePush:
    """POST /api/jobs/cookies/{platform} endpoint tests."""

    def test_push_douyin_cookies_create(self, client, api_auth):
        """Push douyin cookies — provider does not exist yet, auto-create."""
        resp = client.post(
            "/api/jobs/cookies/douyin",
            json={"cookies": SAMPLE_COOKIES, "username": "test_douyin_user"},
            headers=api_auth,
        )
        assert resp.status_code == 200, f"Cookie push failed: {resp.text}"
        data = resp.json()
        assert data["status"] == "ok"
        assert data["platform"] == "douyin"
        assert data["cookie_count"] == 3
        assert data["username"] == "test_douyin_user"

    def test_push_bilibili_cookies_create(self, client, api_auth):
        """Push bilibili cookies — create path."""
        resp = client.post(
            "/api/jobs/cookies/bilibili",
            json={"cookies": [
                {"name": "SESSDATA", "value": "test_sess", "domain": ".bilibili.com"},
                {"name": "bili_jct", "value": "test_jct", "domain": ".bilibili.com"},
            ]},
            headers=api_auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["platform"] == "bilibili"
        assert data["cookie_count"] == 2

    def test_push_cookies_update(self, client, api_auth):
        """Push cookies twice — second call updates existing provider."""
        resp1 = client.post(
            "/api/jobs/cookies/douyin",
            json={"cookies": SAMPLE_COOKIES},
            headers=api_auth,
        )
        assert resp1.status_code == 200

        new_cookies = [{"name": "sid_tt", "value": "updated_value", "domain": ".douyin.com"}]
        resp2 = client.post(
            "/api/jobs/cookies/douyin",
            json={"cookies": new_cookies},
            headers=api_auth,
        )
        assert resp2.status_code == 200
        assert resp2.json()["cookie_count"] == 1

    def test_push_unsupported_platform(self, client, api_auth):
        """Push cookies for unsupported platform → 400."""
        resp = client.post(
            "/api/jobs/cookies/wechat",
            json={"cookies": SAMPLE_COOKIES},
            headers=api_auth,
        )
        assert resp.status_code == 400
        assert "Unsupported platform" in resp.json()["detail"]

    def test_push_empty_cookies(self, client, api_auth):
        """Push empty cookies list → 400."""
        resp = client.post(
            "/api/jobs/cookies/douyin",
            json={"cookies": []},
            headers=api_auth,
        )
        assert resp.status_code == 400
        assert "No cookies" in resp.json()["detail"]

    def test_push_unauthorized(self, client):
        """No auth → 401."""
        resp = client.post(
            "/api/jobs/cookies/douyin",
            json={"cookies": SAMPLE_COOKIES},
        )
        assert resp.status_code == 401
