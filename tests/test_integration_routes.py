"""Integration tests for trending, video, and publish routes.

Tests HTTP-level behavior of /api/trending, /api/jobs/video, /api/jobs/publish
using fastapi.TestClient (same pattern as existing test_auth.py).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


TEST_USER = {
    "username": "integ_test_user",
    "email": "integ@test.com",
    "password": "integpass123",
}


def _client():
    """Return a TestClient instance."""
    return TestClient(app)


def _get_auth_token():
    """Register + login, return JWT token string."""
    client = _client()
    client.post("/api/auth/register", json=TEST_USER)
    resp = client.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


# ── /api/trending tests ──────────────────────────────────────────────────────


class TestTrendingIntegration:
    """Trending proxy endpoints — TrendScope unavailable returns 503."""

    def test_trending_returns_503_when_unavailable(self):
        """GET /api/trending returns 503 when TrendScope is down."""
        client = _client()
        resp = client.get("/api/trending")
        assert resp.status_code == 503
        data = resp.json()
        assert "detail" in data

    def test_trending_platforms_returns_503_when_unavailable(self):
        """GET /api/trending/platforms returns 503 when TrendScope is down."""
        client = _client()
        resp = client.get("/api/trending/platforms")
        assert resp.status_code == 503

    def test_trending_by_platform_returns_503_when_unavailable(self):
        """GET /api/trending/{platform} returns 503 when TrendScope is down."""
        client = _client()
        resp = client.get("/api/trending/weibo")
        assert resp.status_code == 503


# ── /api/jobs/video tests ───────────────────────────────────────────────────


class TestVideoIntegration:
    """Video job endpoints — auth required, returns proper status codes."""

    def test_create_video_unauthenticated(self):
        """POST /api/jobs/video returns 401 without auth."""
        client = _client()
        resp = client.post("/api/jobs/video", json={"article_id": "fake-id"})
        assert resp.status_code == 401

    def test_list_video_jobs_unauthenticated(self):
        """GET /api/jobs/video returns 401 without auth."""
        client = _client()
        resp = client.get("/api/jobs/video")
        assert resp.status_code == 401

    def test_get_video_job_unauthenticated(self):
        """GET /api/jobs/video/{id} returns 401 without auth."""
        client = _client()
        resp = client.get("/api/jobs/video/nonexistent-id")
        assert resp.status_code == 401

    def test_queue_status_authenticated(self):
        """GET /api/jobs/video/queue-status returns 200 with auth."""
        token = _get_auth_token()
        client = _client()
        resp = client.get("/api/jobs/video/queue-status",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "active_tasks" in data
        assert "max_concurrent" in data
        assert "queue_size" in data


# ── /api/jobs/publish tests ─────────────────────────────────────────────────


class TestPublishIntegration:
    """Publish endpoints — auth required, returns proper status codes."""

    def test_create_publish_unauthenticated(self):
        """POST /api/jobs/publish returns 401 without auth."""
        client = _client()
        resp = client.post("/api/jobs/publish", json={
            "article_id": "fake-id",
            "platforms": ["wechat_mp"],
        })
        assert resp.status_code == 401

    def test_list_publish_tasks_unauthenticated(self):
        """GET /api/jobs/publish returns 401 without auth."""
        client = _client()
        resp = client.get("/api/jobs/publish")
        assert resp.status_code == 401

    def test_get_publish_status_unauthenticated(self):
        """GET /api/jobs/publish/{id} returns 401 without auth."""
        client = _client()
        resp = client.get("/api/jobs/publish/nonexistent-id")
        assert resp.status_code == 401

    def test_list_publish_tasks_authenticated(self):
        """GET /api/jobs/publish returns 200 with auth (empty list)."""
        token = _get_auth_token()
        client = _client()
        resp = client.get("/api/jobs/publish",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)
