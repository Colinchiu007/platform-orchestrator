"""TrendScope integration test — validate orchestrator proxy + pipeline entry.

Tests the TrendScope proxy router and the trending_to_pipeline flag without
requiring a running TrendScope instance.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Rate-limit bypass & DB init handled by conftest.py
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# Reusable fake JSON response helper
def _make_mock_json(json_data: dict, status: int = 200):
    """Return an async context-manager mock that simulates httpx response."""

    class _FakeResponse:
        def __init__(self):
            self.status_code = status

        def json(self):
            return json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                raise httpx.HTTPStatusError(
                    "error", request=None, response=self  # type: ignore
                )

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            return _FakeResponse()

    return _FakeClient()


class TestTrendingProxy:
    """Tests for the orchestrator → TrendScope proxy router."""

    @patch("routers.trending.httpx.AsyncClient", autospec=False)
    def test_platforms_returns_data(self, mock_client_class, client):
        """GET /api/trending/platforms proxies and returns platform list."""
        mock_client_class.return_value = _make_mock_json({
            "code": 0,
            "data": {
                "platforms": [
                    {"id": 1, "code": "weibo", "name": "微博",
                     "icon_url": "", "category": "social", "is_active": True},
                    {"id": 2, "code": "zhihu", "name": "知乎",
                     "icon_url": "", "category": "social", "is_active": True},
                ]
            },
        })

        resp = client.get("/api/trending/platforms")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 0
        assert len(data["data"]["platforms"]) == 2

    @patch("routers.trending.httpx.AsyncClient", autospec=False)
    def test_aggregated_trending(self, mock_client_class, client):
        """GET /api/trending proxies aggregated trending."""
        mock_client_class.return_value = _make_mock_json({
            "code": 0,
            "data": {"items": []},
            "pagination": {"page": 1, "page_size": 20, "total": 0, "total_pages": 0},
        })

        resp = client.get("/api/trending")
        assert resp.status_code == 200

    @patch("routers.trending.httpx.AsyncClient", autospec=False)
    def test_platform_trending(self, mock_client_class, client):
        """GET /api/trending/{platform} proxies platform-specific trending."""
        mock_client_class.return_value = _make_mock_json({
            "code": 0,
            "data": {"items": []},
            "pagination": {"page": 1, "page_size": 50, "total": 0, "total_pages": 0},
        })

        resp = client.get("/api/trending/weibo")
        assert resp.status_code == 200

    @patch("routers.trending.httpx.AsyncClient", autospec=False)
    def test_trendscope_unavailable_returns_503(self, mock_client_class, client):
        """When TrendScope is down, proxy returns 503."""
        import httpx

        class _FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                raise httpx.ConnectError("Connection refused")

        mock_client_class.return_value = _FailingClient()

        resp = client.get("/api/trending")
        assert resp.status_code == 503
        assert "不可用" in resp.json()["detail"]


class TestFeatureGates:
    """Feature gates should load from the local feature_gates.yaml."""

    def test_features_endpoint(self, client):
        """GET /api/features lists all gates."""
        resp = client.get("/api/features")
        assert resp.status_code == 200
        features = resp.json()["features"]
        assert "trending_feed" in features
        assert features["trending_feed"]["enabled"] is True
        assert "trending_to_pipeline" in features
        assert features["trending_to_pipeline"]["enabled"] is True
