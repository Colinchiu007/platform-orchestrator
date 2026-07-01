"""Tests for unified jobs and user_settings endpoints.

Tests:
- GET    /api/jobs/ — list all jobs
- GET    /api/jobs/detail/{job_id} — job detail
- POST   /api/jobs/detail/{job_id}/retry — retry failed job
- GET    /api/settings/profile — user profile
- PATCH  /api/settings/profile — update profile
- GET    /api/settings/api-keys — list API keys
- POST   /api/settings/api-keys — create API key
- DELETE /api/settings/api-keys/{key_id} — delete API key
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from middleware.rate_limit import reset_rate_limits

reset_rate_limits()

from main import app

client = TestClient(app)

# Shared user for tests — register once, use everywhere
_SHARED_AUTH = None


def _get_shared_auth() -> dict:
    global _SHARED_AUTH
    if _SHARED_AUTH is not None:
        return _SHARED_AUTH
    # Register + login once
    username = "align_shared"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "test123",
    })
    assert resp.status_code in (201, 409)
    resp = client.post("/api/auth/login", json={
        "username": username,
        "password": "test123",
    })
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]
    _SHARED_AUTH = {"Authorization": f"Bearer {token}"}
    return _SHARED_AUTH


def _register_unique(prefix="u") -> dict:
    """Register a unique user. Resets rate limits first."""
    reset_rate_limits()
    import random
    suffix = random.randint(10000, 99999)
    username = f"{prefix}_{suffix}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "test123",
    })
    assert resp.status_code in (201, 409), f"Register failed: {resp.text}"
    resp = client.post("/api/auth/login", json={
        "username": username,
        "password": "test123",
    })
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── Tests: /api/settings/profile ─────────────────────────────


class TestProfile:
    def test_get_profile(self):
        auth = _register_unique("prof")
        resp = client.get("/api/settings/profile", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert "username" in data
        assert "email" in data
        assert "role" in data
        assert "id" in data
        assert "created_at" in data

    def test_get_profile_unauthorized(self):
        resp = client.get("/api/settings/profile")
        assert resp.status_code == 401

    def test_update_profile(self):
        auth = _register_unique("upd")
        import random
        _suff = random.randint(10000, 99999)
        resp = client.patch("/api/settings/profile", json={
            "username": f"new_upd_{_suff}",
            "email": f"new_upd_{_suff}@test.com",
        }, headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == f"new_upd_{_suff}"
        assert data["email"] == f"new_upd_{_suff}@test.com"

        # Verify persisted
        resp2 = client.get("/api/settings/profile", headers=auth)
        assert resp2.json()["username"] == f"new_upd_{_suff}"

    def test_update_profile_unauthorized(self):
        resp = client.patch("/api/settings/profile", json={"username": "x"})
        assert resp.status_code == 401

    def test_update_profile_duplicate_username(self):
        auth1 = _register_unique("dup_a")
        auth2 = _register_unique("dup_b")
        # Get auth1's username
        prof1 = client.get("/api/settings/profile", headers=auth1).json()
        resp = client.patch("/api/settings/profile", json={
            "username": prof1["username"],
        }, headers=auth2)
        assert resp.status_code == 409


# ── Tests: /api/settings/api-keys ────────────────────────────


class TestApiKeys:
    def _auth(self):
        return _get_shared_auth()

    def test_list_keys_empty(self):
        auth = _register_unique("key_e")
        resp = client.get("/api/settings/api-keys", headers=auth)
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_create_key(self):
        auth = _register_unique("key_c")
        resp = client.post("/api/settings/api-keys", json={
            "label": "My Test Key",
        }, headers=auth)
        assert resp.status_code == 201
        data = resp.json()
        assert data["label"] == "My Test Key"
        assert data["key_preview"].startswith("...")
        assert len(data["key"]) == 64

    def test_create_and_list_keys(self):
        auth = _register_unique("key_l")
        client.post("/api/settings/api-keys", json={"label": "Key 1"}, headers=auth)
        client.post("/api/settings/api-keys", json={"label": "Key 2"}, headers=auth)

        resp = client.get("/api/settings/api-keys", headers=auth)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        labels = [i["label"] for i in items]
        assert "Key 1" in labels
        assert "Key 2" in labels

    def test_delete_key(self):
        auth = _register_unique("key_d")
        created = client.post("/api/settings/api-keys", json={"label": "To Delete"}, headers=auth)
        key_id = created.json()["id"]

        resp = client.delete(f"/api/settings/api-keys/{key_id}", headers=auth)
        assert resp.status_code == 204

        resp2 = client.get("/api/settings/api-keys", headers=auth)
        assert len(resp2.json()["items"]) == 0

    def test_delete_key_not_found(self):
        auth = _register_unique("key_404")
        resp = client.delete("/api/settings/api-keys/nonexistent-id", headers=auth)
        assert resp.status_code == 404

    def test_create_key_unauthorized(self):
        resp = client.post("/api/settings/api-keys", json={"label": "x"})
        assert resp.status_code == 401


# ── Tests: /api/jobs ─────────────────────────────────────────


class TestJobsList:
    def _auth(self):
        return _get_shared_auth()

    def test_list_jobs(self):
        resp = client.get("/api/jobs/", headers=self._auth())
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_list_jobs_unauthorized(self):
        resp = client.get("/api/jobs/")
        assert resp.status_code == 401


class TestJobDetail:
    def test_get_job_not_found(self):
        auth = _register_unique("jd_404")
        resp = client.get("/api/jobs/detail/nonexistent-job-id", headers=auth)
        assert resp.status_code == 404

    def test_get_job_unauthorized(self):
        resp = client.get("/api/jobs/detail/some-id")
        assert resp.status_code == 401


class TestJobRetry:
    def _auth(self):
        return _get_shared_auth()

    def test_retry_no_job(self):
        resp = client.post("/api/jobs/detail/nonexistent/retry", headers=self._auth())
        assert resp.status_code == 404

    def test_retry_unauthorized(self):
        resp = client.post("/api/jobs/detail/some-id/retry")
        assert resp.status_code == 401

    def test_retry_job_success(self):
        """Insert a failed job and verify retry resets it."""
        import aiosqlite
        from db import DB_PATH

        auth = self._auth()
        me = client.get("/api/auth/me", headers=auth).json()
        uid = me["uuid"]

        job_id = f"retry-succ-{uuid.uuid4().hex[:8]}"
        async def _setup():
            db = await aiosqlite.connect(DB_PATH)
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                "INSERT OR IGNORE INTO jobs (id, user_id, job_type, status, error) "
                "VALUES (?, ?, 'test', 'failed', 'intentional')",
                (job_id, uid),
            )
            await db.commit()
            await db.close()
        asyncio.run(_setup())

        resp = client.post(f"/api/jobs/detail/{job_id}/retry", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

        async def _check():
            db = await aiosqlite.connect(DB_PATH)
            cur = await db.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,))
            row = await cur.fetchone()
            await db.close()
            return {"status": row[0], "error": row[1]}
        row = asyncio.run(_check())
        assert row["status"] == "pending"
        assert row["error"] is None

    def test_retry_non_failed_job(self):
        import aiosqlite
        from db import DB_PATH

        auth = self._auth()
        me = client.get("/api/auth/me", headers=auth).json()
        uid = me["uuid"]

        job_id = f"retry-nf-{uuid.uuid4().hex[:8]}"
        async def _setup():
            db = await aiosqlite.connect(DB_PATH)
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                "INSERT OR IGNORE INTO jobs (id, user_id, job_type, status) "
                "VALUES (?, ?, 'test', 'success')",
                (job_id, uid),
            )
            await db.commit()
            await db.close()
        asyncio.run(_setup())

        resp = client.post(f"/api/jobs/detail/{job_id}/retry", headers=auth)
        assert resp.status_code == 400


class TestJobsWithRealJob:
    """Integration tests with actual jobs in DB."""

    def _auth(self):
        return _get_shared_auth()

    def test_jobs_list_and_detail_after_insert(self):
        import aiosqlite
        from db import DB_PATH

        auth = self._auth()
        me = client.get("/api/auth/me", headers=auth).json()
        uid = me["uuid"]

        jid = f"real-list-{uuid.uuid4().hex[:8]}"
        async def _setup():
            db = await aiosqlite.connect(DB_PATH)
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                "INSERT OR IGNORE INTO jobs (id, user_id, job_type, status, input_data) "
                "VALUES (?, ?, 'video', 'done', ?)",
                (jid, uid, json.dumps({"article_id": "art_1"})),
            )
            await db.commit()
            await db.close()
        asyncio.run(_setup())

        # List
        resp = client.get("/api/jobs/", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

        # Detail
        resp = client.get(f"/api/jobs/detail/{jid}", headers=auth)
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["id"] == jid
        assert detail["job_type"] == "video"
        assert "input_data" in detail
        assert "output_data" in detail

        # Wrong user gets 404
        other = _register_unique("other")
        resp = client.get(f"/api/jobs/detail/{jid}", headers=other)
        assert resp.status_code == 404

    def test_jobs_list_mixed_types(self):
        import aiosqlite
        from db import DB_PATH

        auth = self._auth()
        me = client.get("/api/auth/me", headers=auth).json()
        uid = me["uuid"]

        job_types = [("video", "done"), ("publish", "success"), ("video_publish", "pending")]
        async def _setup():
            db = await aiosqlite.connect(DB_PATH)
            await db.execute("PRAGMA journal_mode=WAL;")
            import uuid as _u
            for i, (jt, st) in enumerate(job_types):
                jid = f"mixed-{_u.uuid4().hex[:8]}"
                await db.execute(
                    "INSERT OR IGNORE INTO jobs (id, user_id, job_type, status) "
                    "VALUES (?, ?, ?, ?)",
                    (jid, uid, jt, st),
                )
            await db.commit()
            await db.close()
        asyncio.run(_setup())

        resp = client.get("/api/jobs/", headers=auth)
        assert resp.status_code == 200
        types = [item["job_type"] for item in resp.json()["items"]]
        for jt, _ in job_types:
            assert jt in types
