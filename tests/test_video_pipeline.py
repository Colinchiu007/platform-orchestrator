"""Tests for video pipeline endpoints — article fetch → split → video job.

Mocks all external services (trafilatura, httpx, LLM, SmartSentenceSplitter).
Background pipeline tasks are suppressed to isolate endpoint behaviour.
"""

from __future__ import annotations

# Initialize DB once at module level (idempotent)
import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from db import init_db
from middleware.rate_limit import reset_rate_limits

asyncio.run(init_db())

# ── Monkeypatch rate_limit_video BEFORE routers import it ────────────────
# slowapi's LimitGroup.__iter__ calls limit_provider() without arguments
# when the function has no `key` parameter. We provide a no-arg lambda
# so all video-route tests bypass rate-limit evaluation.
import middleware.rate_limit as _rl_mod

_rl_mod.rate_limit_video = lambda: "1000/hour"

from main import app  # noqa: E402 — must run after monkeypatch

TEST_USER = {"username": "videopipe_user", "email": "videopipe@example.com", "password": "testpass123"}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _register_and_login(client, username="videopipe_user", password="testpass123") -> dict:
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


def _upgrade_to_pro(client, auth_header: dict):
    """Upgrade user to pro tier (tier 2) so video endpoints are accessible."""
    resp = client.post("/api/auth/upgrade", json={"plan": "pro"}, headers=auth_header)
    assert resp.status_code == 200, f"Upgrade failed: {resp.text}"


def _login_after_upgrade(client, username="videopipe_user", password="testpass123") -> dict:
    """Login again after upgrading — JWT must reflect the new tier."""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Re-login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_mock_split_result():
    """Build a MagicMock that satisfies the SplitResult interface used by the router."""
    # ── subtitles ──
    sub = MagicMock()
    sub.text = "Test scene subtitle."
    sub.start_time = 0.0
    sub.duration = 5.0
    sub.display_order = 0

    # ── scene ──
    scene = MagicMock()
    scene.segment_id = 0
    scene.text = "Test scene text."
    scene.estimated_duration = 5.0
    scene.target_words = 10
    scene.subtitles = [sub]

    # ── sentence ──
    sent = MagicMock()
    sent.index = 0
    sent.text = "Test sentence."
    sent.char_count = 15
    sent.language = "en"
    sent.tier = "tier1_rule"

    # ── top-level result ──
    result = MagicMock()
    result.to_dict.return_value = {
        "sentences": [{"index": 0, "text": "Test sentence.", "char_count": 15, "language": "en", "tier": "tier1_rule"}],
        "scenes": [{
            "segment_id": 0, "text": "Test scene text.", "estimated_duration": 5.0, "target_words": 10,
            "subtitles": [{"text": "Test scene subtitle.", "start_time": 0.0, "duration": 5.0, "display_order": 0}],
        }],
        "tier_used": "tier3_rule", "language": "en", "total_duration": 5.0, "total_words": 15,
    }
    result.language = "en"
    result.tier_used = "tier3_rule"
    result.total_scenes = 1
    result.total_duration = 5.0
    result.total_words = 15
    result.scenes = [scene]
    result.sentences = [sent]
    return result


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_tables():
    """Remove test data between runs to guarantee isolation."""
    conn = sqlite3.connect("orchestrator.db")
    try:
        conn.execute("DELETE FROM refresh_tokens")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM articles")
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


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestVideoPipeline:
    """Video job creation endpoint tests — POST /api/jobs/video."""

    @patch("routers.aggregator.collect_url")
    @patch("routers.splitter._splitter.split")
    @patch("routers.video._run_video_pipeline")  # suppress background task
    def test_full_pipeline(self, mock_pipeline: MagicMock, mock_split: MagicMock, mock_collect: AsyncMock, client):
        """POST /api/articles/fetch → split → video → verify job created with 'queued' status."""
        # ── Arrange mocks ──
        mock_collect.return_value = MagicMock(
            title="Test Article", content="Content for video testing. " * 5,
            author="Tester", word_count=100, source_url="https://example.com/article",
        )
        mock_split.return_value = _make_mock_split_result()

        # ── Authenticate & upgrade ──
        auth = _register_and_login(client)
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client)  # fresh JWT with tier=2

        # ── Step 1: Fetch article ──
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/article",
        }, headers=auth)
        assert resp.status_code == 200, f"Fetch failed: {resp.text}"
        article_id = resp.json()["article_id"]
        assert article_id is not None

        # ── Step 2: Split article ──
        resp = client.post(f"/api/articles/{article_id}/split", headers=auth)
        assert resp.status_code == 200, f"Split failed: {resp.text}"
        data = resp.json()
        assert data["total_scenes"] == 1
        assert len(data["scenes"]) == 1

        # ── Step 3: Create video job ──
        resp = client.post("/api/jobs/video", json={
            "article_id": article_id,
        }, headers=auth)
        assert resp.status_code == 200, f"Video job creation failed: {resp.text}"
        job = resp.json()
        assert "job_id" in job
        assert job["status"] == "queued"
        assert "message" in job

        # ── Step 4: Verify job exists via GET ──
        resp = client.get(f"/api/jobs/video/{job['job_id']}", headers=auth)
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["job_id"] == job["job_id"]
        assert detail["status"] == "queued"

        # Background task was added (TestClient executes it as a no-op mock)
        assert mock_pipeline.called

    def test_invalid_article_for_video(self, client):
        """POST /api/jobs/video with a non-existent article_id → 404."""
        auth = _register_and_login(client)
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client)  # fresh JWT with tier=2

        resp = client.post("/api/jobs/video", json={
            "article_id": "non-existent-id",
        }, headers=auth)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @patch("routers.aggregator.collect_url")
    def test_missing_split_before_video(self, mock_collect: AsyncMock, client):
        """Article exists but has no split result → 400 on video creation."""
        mock_collect.return_value = MagicMock(
            title="No Split Article", content="Some content without split. " * 3,
            author="Tester", word_count=30, source_url="https://example.com/no-split",
        )

        auth = _register_and_login(client)
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client)

        # Create article (no split)
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/no-split",
        }, headers=auth)
        assert resp.status_code == 200
        article_id = resp.json()["article_id"]

        # Try to create video without splitting first
        resp = client.post("/api/jobs/video", json={
            "article_id": article_id,
        }, headers=auth)
        assert resp.status_code == 400
        assert "split" in resp.json()["detail"].lower()

    def test_unauthorized_pipeline_access(self, client):
        """No auth token → 401."""
        resp = client.post("/api/jobs/video", json={
            "article_id": "any-id",
        })
        assert resp.status_code == 401

    @patch("routers.aggregator.collect_url")
    @patch("routers.splitter._splitter.split")
    @patch("routers.video._run_video_pipeline")
    def test_video_job_list(self, mock_pipeline: MagicMock, mock_split: MagicMock, mock_collect: AsyncMock, client):
        """GET /api/jobs/video/ returns user's video jobs (bonus test)."""
        mock_collect.return_value = MagicMock(
            title="List Test", content="Content for list test. " * 5,
            author="Tester", word_count=50, source_url="https://example.com/list",
        )
        mock_split.return_value = _make_mock_split_result()

        auth = _register_and_login(client)
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client)

        # Create article + split + video
        resp = client.post("/api/articles/fetch", json={"url": "https://example.com/list"}, headers=auth)
        article_id = resp.json()["article_id"]
        client.post(f"/api/articles/{article_id}/split", headers=auth)
        client.post("/api/jobs/video", json={"article_id": article_id}, headers=auth)

        # List jobs
        resp = client.get("/api/jobs/video/", headers=auth)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
