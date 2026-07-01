"""Tests for GET /api/jobs/stats — daily job statistics aggregation."""
from __future__ import annotations

import uuid
import pytest
from fastapi.testclient import TestClient
from middleware.rate_limit import reset_rate_limits

reset_rate_limits()
from main import app

client = TestClient(app)

_SHARED_AUTH = None


def _get_auth() -> dict:
    global _SHARED_AUTH
    if _SHARED_AUTH is not None:
        return _SHARED_AUTH
    username = "stats_test"
    resp = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@test.com", "password": "test123",
    })
    assert resp.status_code in (201, 409)
    resp = client.post("/api/auth/login", json={
        "username": username, "password": "test123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    _SHARED_AUTH = {"Authorization": f"Bearer {token}"}
    return _SHARED_AUTH


class TestJobStats:
    """GET /api/jobs/stats — 7-day trend aggregation."""

    def test_returns_empty_when_no_jobs(self):
        """No jobs → empty daily array."""
        resp = client.get("/api/jobs/stats", headers=_get_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert "daily" in data
        assert "totals" in data
        assert len(data["daily"]) == 7  # always 7 days
        assert data["totals"] == {"pending": 0, "processing": 0, "completed": 0, "failed": 0}

    def test_aggregates_jobs_correctly(self):
        """Jobs spread across days produce correct daily counts."""
        auth = _get_auth()
        from datetime import datetime, timedelta
        import sqlite3
        from db import DB_PATH

        # Get actual user_id from profile
        profile_resp = client.get("/api/settings/profile", headers=auth)
        assert profile_resp.status_code == 200
        user_id = profile_resp.json()["id"]

        # Insert test jobs across 3 days with unique IDs
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        today = datetime.now()
        tag = uuid.uuid4().hex[:8]
        
        for day_offset, status, count in [
            (0, "completed", 3), (0, "failed", 1),
            (1, "completed", 2), (1, "processing", 1),
            (2, "pending", 4),
        ]:
            day = today - timedelta(days=day_offset)
            day_str = day.strftime("%Y-%m-%d")
            for i in range(count):
                job_id = f"agg_{tag}_{day_offset}_{status}_{i}"
                created = f"{day_str}T{10+i:02d}:00:00"
                cursor.execute(
                    "INSERT OR IGNORE INTO jobs (id, user_id, job_type, status, created_at) VALUES (?, ?, 'video', ?, ?)",
                    (job_id, user_id, status, created),
                )
        conn.commit()
        conn.close()

        resp = client.get("/api/jobs/stats", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["daily"]) == 7

        # Check aggregates for last 3 days
        for day in data["daily"]:
            if day["date"] == today.strftime("%Y-%m-%d"):
                assert day["completed"] >= 3
                assert day["failed"] >= 1
            elif day["date"] == (today - timedelta(days=1)).strftime("%Y-%m-%d"):
                assert day["completed"] >= 2
                assert day["processing"] >= 1
            elif day["date"] == (today - timedelta(days=2)).strftime("%Y-%m-%d"):
                assert day["pending"] >= 4

        assert data["totals"]["completed"] >= 5
        assert data["totals"]["failed"] >= 1

    def test_requires_auth(self):
        """401 when no token."""
        resp = client.get("/api/jobs/stats")
        assert resp.status_code == 401

    def test_respects_days_param(self):
        """?days=14 returns 14 days."""
        resp = client.get("/api/jobs/stats?days=14", headers=_get_auth())
        assert resp.status_code == 200
        assert len(resp.json()["daily"]) == 14

    def test_invalid_days_param(self):
        """?days=abc returns 422."""
        resp = client.get("/api/jobs/stats?days=abc", headers=_get_auth())
        assert resp.status_code == 422
