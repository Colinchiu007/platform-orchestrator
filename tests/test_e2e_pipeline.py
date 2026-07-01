"""End-to-end pipeline tests — multi-user isolation, full user journey.

Mocks all external services (trafilatura, httpx, LLM, SmartSentenceSplitter).
Background pipeline tasks are suppressed to isolate endpoint behaviour.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# DB init is handled by conftest.py session fixture — no module-level asyncio.run()
# Rate-limit bypass is handled by conftest.py session fixture
from main import app  # noqa: E402 — conftest handles monkeypatch before this import
from middleware.rate_limit import reset_rate_limits

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


def _upgrade_to_pro(client, auth_header: dict):
    """Upgrade user to pro tier (tier 2) so video endpoints are accessible."""
    resp = client.post("/api/auth/upgrade", json={"plan": "pro"}, headers=auth_header)
    assert resp.status_code == 200, f"Upgrade failed: {resp.text}"


def _login_after_upgrade(client, username: str, password: str = "testpass123") -> dict:
    """Login again after upgrading — JWT must reflect the new tier."""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Re-login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_mock_split_result():
    """Build a MagicMock that satisfies the SplitResult interface used by the router."""
    sub = MagicMock()
    sub.text = "E2E scene subtitle."
    sub.start_time = 0.0
    sub.duration = 5.0
    sub.display_order = 0

    scene = MagicMock()
    scene.segment_id = 0
    scene.text = "E2E scene text for full pipeline testing."
    scene.estimated_duration = 5.0
    scene.target_words = 15
    scene.subtitles = [sub]

    sent = MagicMock()
    sent.index = 0
    sent.text = "E2E sentence."
    sent.char_count = 14
    sent.language = "en"
    sent.tier = "tier1_rule"

    result = MagicMock()
    result.to_dict.return_value = {
        "sentences": [{"index": 0, "text": "E2E sentence.", "char_count": 14, "language": "en", "tier": "tier1_rule"}],
        "scenes": [{
            "segment_id": 0, "text": "E2E scene text for full pipeline testing.",
            "estimated_duration": 5.0, "target_words": 15,
            "subtitles": [{"text": "E2E scene subtitle.", "start_time": 0.0, "duration": 5.0, "display_order": 0}],
        }],
        "tier_used": "tier3_rule", "language": "en", "total_duration": 5.0, "total_words": 14,
    }
    result.language = "en"
    result.tier_used = "tier3_rule"
    result.total_scenes = 1
    result.total_duration = 5.0
    result.total_words = 14
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


class TestE2EPipeline:
    """End-to-end pipeline tests for the full user journey."""

    @patch("routers.aggregator.collect_url")
    @patch("routers.splitter._splitter.split")
    @patch("routers.prompt.optimize_prompt_service")
    @patch("routers.video._run_video_pipeline")
    def test_full_user_journey(
        self,
        mock_pipeline: MagicMock,
        mock_prompt_svc: AsyncMock,
        mock_split: MagicMock,
        mock_collect: AsyncMock,
        client,
    ):
        """register → login → fetch article → split → prompt → create video → check status."""
        # ── Arrange mocks ──
        mock_collect.return_value = MagicMock(
            title="E2E Article", content="E2E test content for full journey. " * 10,
            author="E2E Tester", word_count=80,
            source_url="https://example.com/e2e-article",
        )
        mock_split.return_value = _make_mock_split_result()
        mock_prompt_svc.return_value = MagicMock(
            prompts=["A cinematic scene of a beautiful landscape at sunset with warm golden lighting."],
        )

        # ── Register & login ──
        auth = _register_and_login(client, "e2e_user")
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client, "e2e_user")

        # ── 1. Fetch article (no rewrite — avoid external LLM dependency) ──
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/e2e-article",
        }, headers=auth)
        assert resp.status_code == 200, f"Fetch failed: {resp.text}"
        article_id = resp.json()["article_id"]

        # ── 2. Split article ──
        resp = client.post(f"/api/articles/{article_id}/split", headers=auth)
        assert resp.status_code == 200, f"Split failed: {resp.text}"
        split_data = resp.json()
        assert split_data["total_scenes"] == 1
        assert split_data["language"] == "en"

        # ── 3. Optimize prompt ──
        resp = client.post("/api/prompts/optimize", json={
            "scene_text": "A beautiful landscape at sunset.",
        }, headers=auth)
        assert resp.status_code == 200, f"Prompt optimize failed: {resp.text}"
        prompt_data = resp.json()
        assert "prompts" in prompt_data
        assert len(prompt_data["prompts"]) == 1
        assert "cinematic" in prompt_data["prompts"][0].lower()

        # ── 4. Create video job ──
        resp = client.post("/api/jobs/video", json={
            "article_id": article_id,
        }, headers=auth)
        assert resp.status_code == 200, f"Video job failed: {resp.text}"
        job = resp.json()
        assert job["status"] in ("queued", "processing")
        assert "job_id" in job

        # ── 5. Check video job status ──
        resp = client.get(f"/api/jobs/video/{job['job_id']}", headers=auth)
        assert resp.status_code == 200
        status = resp.json()
        assert status["job_id"] == job["job_id"]
        assert "status" in status

    @patch("routers.aggregator.collect_url")
    def test_multi_user_isolation(self, mock_collect: AsyncMock, client):
        """User A creates article, User B cannot GET it (404)."""
        mock_collect.return_value = MagicMock(
            title="Isolation Article", content="Content for isolation test. " * 5,
            author="UserA", word_count=40, source_url="https://example.com/iso",
        )

        # User A registers and creates article
        auth_a = _register_and_login(client, "user_a")
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/iso",
        }, headers=auth_a)
        assert resp.status_code == 200
        article_id = resp.json()["article_id"]

        # User B registers and tries to access User A's article
        auth_b = _register_and_login(client, "user_b")
        resp = client.get(f"/api/articles/{article_id}", headers=auth_b)
        assert resp.status_code == 404, "User B should not see User A's article"

    @patch("routers.aggregator.collect_url")
    def test_user_b_cannot_delete_user_a_article(self, mock_collect: AsyncMock, client):
        """User B attempts to access/modify User A's data → 404 (user isolation).

        Note: There is no DELETE endpoint in the API. This test verifies that
        all user-scoped operations reject cross-user access with 404.
        """
        mock_collect.return_value = MagicMock(
            title="UserA Article", content="Content belonging to User A. " * 5,
            author="UserA", word_count=40, source_url="https://example.com/user-a",
        )

        # User A creates article
        auth_a = _register_and_login(client, "user_a_only")
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/user-a",
        }, headers=auth_a)
        assert resp.status_code == 200
        article_id = resp.json()["article_id"]

        # User B registers and tries to:
        auth_b = _register_and_login(client, "user_b_only")

        # 1. GET User A's article → 404
        resp = client.get(f"/api/articles/{article_id}", headers=auth_b)
        assert resp.status_code == 404, "User B should get 404 on User A article GET"

        # 2. POST split on User A's article → 404
        resp = client.post(f"/api/articles/{article_id}/split", headers=auth_b)
        assert resp.status_code == 404, "User B should get 404 on User A split"

        # 3. POST publish on User A's article → 404
        resp = client.post("/api/jobs/publish", json={
            "article_id": article_id,
        }, headers=auth_b)
        assert resp.status_code == 404, "User B should get 404 on User A publish"

    @patch("routers.aggregator.collect_url")
    @patch("routers.splitter._splitter.split")
    @patch("routers.video._run_video_pipeline")
    @patch("routers.publish._publish_wechat")
    def test_publish_after_video(
        self,
        mock_publish: MagicMock,
        mock_video_pipeline: MagicMock,
        mock_split: MagicMock,
        mock_collect: AsyncMock,
        client,
    ):
        """After article → split → video, publish endpoint creates a publish task (200)."""
        mock_collect.return_value = MagicMock(
            title="Publish Test", content="Content for publish-after-video test. " * 8,
            author="Tester", word_count=60, source_url="https://example.com/publish",
        )
        mock_split.return_value = _make_mock_split_result()

        auth = _register_and_login(client, "pub_user")
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client, "pub_user")

        # 1. Fetch article
        resp = client.post("/api/articles/fetch", json={
            "url": "https://example.com/publish",
        }, headers=auth)
        article_id = resp.json()["article_id"]

        # 2. Split
        client.post(f"/api/articles/{article_id}/split", headers=auth)

        # 3. Create video job
        resp = client.post("/api/jobs/video", json={
            "article_id": article_id,
        }, headers=auth)
        assert resp.status_code == 200
        video_job_id = resp.json()["job_id"]

        # 4. Create publish task
        resp = client.post("/api/jobs/publish", json={
            "article_id": article_id,
        }, headers=auth)
        assert resp.status_code == 200, f"Publish failed: {resp.text}"
        pub = resp.json()
        assert "task_id" in pub
        assert pub["status"] == "pending"
        assert pub["platforms"] == ["wechat_mp"]

        # 5. Verify publish task status
        resp = client.get(f"/api/jobs/publish/{pub['task_id']}", headers=auth)
        assert resp.status_code == 200
        pub_status = resp.json()
        assert pub_status["task_id"] == pub["task_id"]
        assert pub_status["status"] == "pending"

    @patch("routers.aggregator.collect_url")
    @patch("routers.splitter._splitter.split")
    @patch("routers.video._run_video_pipeline")
    def test_video_job_with_image_effect(
        self,
        mock_pipeline: MagicMock,
        mock_split: MagicMock,
        mock_collect: AsyncMock,
        client,
    ):
        """Video job with custom image_effect and transition parameters (bonus test)."""
        mock_collect.return_value = MagicMock(
            title="Effect Test", content="Content for effect params test. " * 5,
            author="Tester", word_count=40, source_url="https://example.com/effect",
        )
        mock_split.return_value = _make_mock_split_result()

        auth = _register_and_login(client, "effect_user")
        _upgrade_to_pro(client, auth)
        auth = _login_after_upgrade(client, "effect_user")

        resp = client.post("/api/articles/fetch", json={"url": "https://example.com/effect"}, headers=auth)
        article_id = resp.json()["article_id"]
        client.post(f"/api/articles/{article_id}/split", headers=auth)

        resp = client.post("/api/jobs/video", json={
            "article_id": article_id,
            "image_effect": "ken-burns",
            "transition": "crossfade",
        }, headers=auth)
        assert resp.status_code == 200
        job = resp.json()
        assert job["status"] in ("queued", "processing")

        # Verify input_data stored in DB reflects the custom params
        resp = client.get(f"/api/jobs/video/{job['job_id']}", headers=auth)
        assert resp.status_code == 200
        detail = resp.json()
        input_data = detail.get("input_data", {})
        assert input_data.get("image_effect") == "ken-burns"
        assert input_data.get("transition") == "crossfade"
