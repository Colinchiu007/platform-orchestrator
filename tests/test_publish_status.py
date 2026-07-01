"""
Tests for PUT /api/jobs/publish/{task_id}/status endpoint.

Covers status transitions:
- pending → downloading → publishing → success/failed
- Invalid status values → 400
- Non-existent task → 404

Uses API calls to seed test data (avoids sqlite3/aiosqlite WAL conflicts).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app

API_PREFIX = "/api/jobs"

TEST_USER = {
    "username": "publish_status_user",
    "email": "publish_status@test.com",
    "password": "testpass123",
}


def _client() -> TestClient:
    return TestClient(app)


def _register_and_login() -> str:
    """Register + login, return JWT token."""
    c = _client()
    c.post("/api/auth/register", json=TEST_USER)
    resp = c.post("/api/auth/login", json={
        "username": TEST_USER["username"],
        "password": TEST_USER["password"],
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _create_publish_task(token: str) -> str:
    """Create a video_publish job via API, return task_id."""
    c = _client()
    resp = c.post(
        f"{API_PREFIX}/publish-video",
        json={
            "video_url": "https://example.com/test.mp4",
            "title": "TDD Test Video",
            "platform": "bilibili",
            "desc": "Test description",
            "tags": ["test"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()["task_id"]


class TestPublishStatusUpdate:
    """Status update endpoint — no auth required (internal API for Multi-Publish)."""

    TOKEN: str = ""
    CLIENT: TestClient = None

    @classmethod
    def setup_class(cls):
        cls.TOKEN = _register_and_login()
        cls.CLIENT = _client()

    def test_update_to_downloading(self):
        """PUT with status=downloading returns 200."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "downloading",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_status"] == "downloading"
        assert data["task_id"] == task_id

    def test_update_to_publishing(self):
        """PUT with status=publishing returns 200."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "publishing",
            "output": {"phase": "publish", "percent": 30},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_status"] == "publishing"

    def test_update_to_success(self):
        """PUT with status=success returns 200 with output_data saved."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "success",
            "output": {
                "platform": "bilibili",
                "publish_id": "BV1GJ411x8x",
                "url": "https://bilibili.com/video/BV1GJ411x8x",
                "duration": 12.5,
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_status"] == "success"

    def test_update_to_failed(self):
        """PUT with status=failed and error message returns 200."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "failed",
            "error": "B站 preupload 403 Forbidden",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_status"] == "failed"

    def test_invalid_status_returns_400(self):
        """PUT with invalid status returns 400."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "invalid_status_xyz",
        })
        assert resp.status_code == 400

    def test_nonexistent_task_returns_404(self):
        """PUT with non-existent task_id returns 404."""
        resp = self.CLIENT.put(
            f"{API_PREFIX}/publish/nonexistent-task-id/status",
            json={"status": "downloading"},
        )
        assert resp.status_code == 404

    def test_full_lifecycle(self):
        """E2E: pending → downloading → publishing → success."""
        task_id = _create_publish_task(self.TOKEN)

        r1 = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "downloading", "output": {"percent": 10},
        })
        assert r1.status_code == 200

        r2 = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "publishing", "output": {"percent": 30},
        })
        assert r2.status_code == 200

        r3 = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "success",
            "output": {"platform": "bilibili", "publish_id": "BV1xxx"},
        })
        assert r3.status_code == 200

    def test_update_without_output(self):
        """PUT status update works with empty/omitted output."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "publishing",
        })
        assert resp.status_code == 200
        assert resp.json()["new_status"] == "publishing"

    def test_no_auth_required(self):
        """PUT status works without auth header."""
        task_id = _create_publish_task(self.TOKEN)
        resp = self.CLIENT.put(f"{API_PREFIX}/publish/{task_id}/status", json={
            "status": "success",
            "output": {"platform": "bilibili", "publish_id": "BV1xxx"},
        })
        assert resp.status_code == 200