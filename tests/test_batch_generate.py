"""Tests for POST /api/v1/aggregator/batch-generate — batch video generation."""
from __future__ import annotations

import uuid
import pytest
from fastapi.testclient import TestClient
from middleware.rate_limit import reset_rate_limits

reset_rate_limits()
from main import app

client = TestClient(app)

_SHARED_AUTH = None
_SHARED_ARTICLE_IDS: list[str] = []


def _setup() -> tuple[dict, list[str]]:
    """Register user + create test articles. Returns (auth_headers, article_ids)."""
    global _SHARED_AUTH, _SHARED_ARTICLE_IDS
    if _SHARED_AUTH is not None and _SHARED_ARTICLE_IDS:
        return _SHARED_AUTH, _SHARED_ARTICLE_IDS

    # Register/log in
    tag = uuid.uuid4().hex[:8]
    username = f"batch_{tag}"
    resp = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@test.com", "password": "test123",
    })
    assert resp.status_code in (201, 409)
    resp = client.post("/api/auth/login", json={
        "username": username, "password": "test123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Create test articles via direct DB insert
    import sqlite3
    from db import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get actual user_id from profile
    profile_resp = client.get("/api/settings/profile", headers=auth)
    assert profile_resp.status_code == 200
    user_id = profile_resp.json()["id"]

    article_ids = []
    for i in range(3):
        aid = f"batch_test_{tag}_{i}"
        cursor.execute(
            """INSERT OR IGNORE INTO articles (id, user_id, source_type, source_url, source_content, word_count_original, status)
               VALUES (?, ?, 'url', ?, ?, ?, 'draft')""",
            (aid, user_id, f"https://example.com/batch_{i}", f"Batch test article {i} content here.", 100 + i),
        )
        article_ids.append(aid)
    conn.commit()
    conn.close()

    _SHARED_AUTH = auth
    _SHARED_ARTICLE_IDS = article_ids
    return auth, article_ids


class TestBatchGenerate:
    """POST /api/v1/aggregator/batch-generate — batch video generation."""

    def test_requires_auth(self):
        """No token → 401."""
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": ["fake-id"],
        })
        assert resp.status_code == 401

    def test_single_article_batch(self):
        """Single article in batch works like regular generate."""
        auth, aids = _setup()
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": [aids[0]],
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["results"]) == 1
        assert data["results"][0]["article_id"] == aids[0]
        assert data["results"][0]["status"] == "pending"
        assert data["missing"] is None

    def test_multiple_articles(self):
        """Multiple valid articles → N jobs created."""
        auth, aids = _setup()
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": aids,
            "voice": "zh-CN-YunxiNeural",
            "video_ratio": "16:9",
            "prompt_platform": "flux",
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == len(aids)
        assert len(data["results"]) == len(aids)
        returned_ids = {r["article_id"] for r in data["results"]}
        assert returned_ids == set(aids)

    def test_partial_missing_articles(self):
        """Some IDs don't exist → valid ones processed, missing listed."""
        auth, aids = _setup()
        fake_id = "nonexistent-article-id-for-batch-test"
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": [aids[0], fake_id],
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["results"]) == 1
        assert data["results"][0]["article_id"] == aids[0]
        assert data["missing"] == [fake_id]

    def test_all_missing_returns_empty(self):
        """No valid articles → 200 with empty results + all IDs in missing."""
        auth, _ = _setup()
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": ["fake-1", "fake-2"],
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["results"] == []
        assert data["missing"] == ["fake-1", "fake-2"]

    def test_empty_article_ids_returns_422(self):
        """Empty list → validation error."""
        auth, _ = _setup()
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": [],
        }, headers=auth)
        assert resp.status_code == 422

    def test_max_20_articles_enforced(self):
        """More than 20 article_ids → 422."""
        auth, _ = _setup()
        resp = client.post("/api/v1/aggregator/batch-generate", json={
            "article_ids": [f"id-{i}" for i in range(21)],
        }, headers=auth)
        assert resp.status_code == 422
