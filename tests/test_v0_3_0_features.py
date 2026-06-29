"""Tests for v0.3.0 features: file upload, batch operations, data export."""

from __future__ import annotations

import io
import json

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
    username = "v030_shared"
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


class TestFileUpload:
    """Tests for POST /api/v1/aggregator/upload."""

    def test_upload_txt_file(self):
        auth = _get_auth()
        content = b"Hello, this is a test article for video generation."
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("test.txt", content, "text/plain")},
            headers=auth,
        )
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        data = resp.json()
        assert "article_id" in data
        assert data["filename"] == "test.txt"
        assert data["word_count"] == len(content)
        assert data["status"] == "draft"

    def test_upload_md_file(self):
        auth = _get_auth()
        content = b"# Markdown Title\n\nSome **bold** content here."
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("test.md", content, "text/markdown")},
            headers=auth,
        )
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        data = resp.json()
        assert "article_id" in data

    def test_upload_unsupported_type(self):
        auth = _get_auth()
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("test.pdf", b"fake pdf content", "application/pdf")},
            headers=auth,
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.text

    def test_upload_no_auth(self):
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("test.txt", b"content", "text/plain")},
        )
        assert resp.status_code == 401

    def test_upload_then_in_generate_options(self):
        """Uploaded article should appear in generate-options."""
        auth = _get_auth()
        content = b"Article for generate options check."
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("gen_check.txt", content, "text/plain")},
            headers=auth,
        )
        assert resp.status_code == 200
        article_id = resp.json()["article_id"]

        # Check it shows up in generate-options
        resp = client.get("/api/v1/aggregator/generate-options", headers=auth)
        assert resp.status_code == 200
        sources = resp.json().get("content_sources", [])
        ids = [s["id"] for s in sources]
        assert article_id in ids, f"Uploaded article {article_id} not in options {ids}"


class TestBatchDelete:
    """Tests for POST /api/articles/batch-delete."""

    def test_batch_delete(self):
        auth = _get_auth()
        # Upload 2 articles
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("del1.txt", b"delete me 1", "text/plain")},
            headers=auth,
        )
        assert resp.status_code == 200
        id1 = resp.json()["article_id"]
        resp = client.post(
            "/api/v1/aggregator/upload",
            files={"file": ("del2.txt", b"delete me 2", "text/plain")},
            headers=auth,
        )
        assert resp.status_code == 200
        id2 = resp.json()["article_id"]

        # Batch delete
        resp = client.post(
            "/api/articles/batch-delete",
            json={"article_ids": [id1, id2]},
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 2

        # Verify gone
        resp = client.get("/api/v1/aggregator/generate-options", headers=auth)
        sources = resp.json().get("content_sources", [])
        deleted_ids = [s["id"] for s in sources]
        assert id1 not in deleted_ids
        assert id2 not in deleted_ids

    def test_batch_delete_empty(self):
        auth = _get_auth()
        resp = client.post(
            "/api/articles/batch-delete",
            json={"article_ids": []},
            headers=auth,
        )
        assert resp.status_code == 400

    def test_batch_delete_no_auth(self):
        resp = client.post(
            "/api/articles/batch-delete",
            json={"article_ids": ["fake-id"]},
        )
        assert resp.status_code == 401


class TestDataExport:
    """Tests for GET /api/articles/export and GET /api/jobs/export."""

    def test_export_articles_json(self):
        auth = _get_auth()
        resp = client.get("/api/articles/export?format=json", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_export_articles_csv(self):
        auth = _get_auth()
        resp = client.get("/api/articles/export?format=csv", headers=auth)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers.get("content-disposition", "")

    def test_export_jobs_json(self):
        auth = _get_auth()
        resp = client.get("/api/jobs/export?format=json", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_export_jobs_csv(self):
        auth = _get_auth()
        resp = client.get("/api/jobs/export?format=csv", headers=auth)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")

    def test_export_invalid_format(self):
        auth = _get_auth()
        resp = client.get("/api/articles/export?format=xml", headers=auth)
        assert resp.status_code == 422  # FastAPI validation error

    def test_export_no_auth(self):
        resp = client.get("/api/articles/export")
        assert resp.status_code == 401
        resp = client.get("/api/jobs/export")
        assert resp.status_code == 401
