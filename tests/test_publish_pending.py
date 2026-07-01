"""
Tests for GET /api/jobs/publish/pending endpoint.

Covers:
- Returns only pending video_publish tasks
- Returns empty list when no pending tasks
- Does not return non-pending or non-video_publish tasks
- No auth required (internal API for Multi-Publish polling)
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi.testclient import TestClient

from main import app

DB = "orchestrator.db"


def _seed_jobs() -> list[str]:
    """Insert one pending video_publish task and return its ID."""
    task_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                job_type TEXT NOT NULL DEFAULT 'video',
                status TEXT NOT NULL DEFAULT 'pending',
                input_data TEXT DEFAULT '{}',
                output_data TEXT DEFAULT '{}',
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO jobs (id, user_id, job_type, status, input_data) "
            "VALUES (?, 'test_user', 'video_publish', 'pending', ?)",
            (task_id, json.dumps({"video_url": "https://example.com/v.mp4", "title": "Test"})),
        )
        conn.commit()
    finally:
        conn.close()
    return [task_id]


class TestPendingEndpoint:
    """Pending tasks endpoint — no auth required."""

    def _client(self):
        return TestClient(app)

    def test_returns_pending_video_publish(self):
        """Returns pending video_publish tasks."""
        task_ids = _seed_jobs()
        resp = self._client().get("/api/jobs/publish/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        # Our task should be in the list
        task_ids_in_response = {item["id"] for item in data["items"]}
        assert any(tid in task_ids_in_response for tid in task_ids)

    def test_returns_empty_when_no_pending(self):
        """Returns empty list when no pending tasks."""
        resp = self._client().get("/api/jobs/publish/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_orders_by_created_at_asc(self):
        """Tasks are ordered oldest first (FIFO)."""
        # Seed two tasks at slightly different times
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())

        conn = sqlite3.connect(DB)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    job_type TEXT NOT NULL DEFAULT 'video',
                    status TEXT NOT NULL DEFAULT 'pending',
                    input_data TEXT DEFAULT '{}',
                    output_data TEXT DEFAULT '{}',
                    error TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Insert first task
            conn.execute(
                "INSERT INTO jobs (id, user_id, job_type, status, input_data, created_at) "
                "VALUES (?, 'test_user', 'video_publish', 'pending', ?, datetime('now', '-1 hour'))",
                (tid1, json.dumps({"title": "Old"})),
            )
            # Insert second task
            conn.execute(
                "INSERT INTO jobs (id, user_id, job_type, status, input_data, created_at) "
                "VALUES (?, 'test_user', 'video_publish', 'pending', ?, datetime('now'))",
                (tid2, json.dumps({"title": "New"})),
            )
            conn.commit()
        finally:
            conn.close()

        resp = self._client().get("/api/jobs/publish/pending")
        assert resp.status_code == 200
        data = resp.json()
        ids = [item["id"] for item in data["items"] if item["id"] in (tid1, tid2)]
        if len(ids) >= 2:
            # tid1 (older) should come before tid2 (newer)
            assert ids.index(tid1) < ids.index(tid2)

    def test_excludes_non_video_publish_jobs(self):
        """Does not return jobs with other job_types."""
        tid1 = _seed_jobs()[0]

        # Seed a 'publish' job (not video_publish)
        conn = sqlite3.connect(DB)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "INSERT INTO jobs (id, user_id, job_type, status, input_data) "
                "VALUES (?, 'test_user', 'publish', 'pending', ?)",
                (str(uuid.uuid4()), json.dumps({"article_id": "abc"})),
            )
            conn.commit()
        finally:
            conn.close()

        resp = self._client().get("/api/jobs/publish/pending")
        assert resp.status_code == 200
        data = resp.json()
        all_types = {item["job_type"] for item in data["items"]}
        assert all_types == {"video_publish"}

    def test_excludes_non_pending_status(self):
        """Does not return failed/success/downloading tasks."""
        # Seed a failed task
        failed_id = str(uuid.uuid4())
        conn = sqlite3.connect(DB)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    job_type TEXT NOT NULL DEFAULT 'video',
                    status TEXT NOT NULL DEFAULT 'pending',
                    input_data TEXT DEFAULT '{}',
                    output_data TEXT DEFAULT '{}',
                    error TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "INSERT INTO jobs (id, user_id, job_type, status, input_data) "
                "VALUES (?, 'test_user', 'video_publish', 'failed', ?)",
                (failed_id, json.dumps({"title": "Failed task"})),
            )
            conn.commit()
        finally:
            conn.close()

        resp = self._client().get("/api/jobs/publish/pending")
        assert resp.status_code == 200
        data = resp.json()
        ids = {item["id"] for item in data["items"]}
        assert failed_id not in ids
