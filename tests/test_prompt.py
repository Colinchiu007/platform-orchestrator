"""Tests for prompt optimization & classification endpoints.

TDD: RED phase — write failing tests first.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from middleware.auth import create_access_token

client = TestClient(app)


def _valid_token() -> str:
    """Create a valid JWT token for testing."""
    return create_access_token({"sub": "test-user", "username": "test", "tier": 1})


@pytest.fixture(autouse=True)
def _mock_api_key():
    """Ensure openai_api_key is set so service functions don't return early."""
    with patch("config.settings.openai_api_key", "test-api-key-for-tests"):
        yield


# ── Optimize Endpoint ────────────────────────────────────────────────────────


class TestOptimizeEndpoint:
    """POST /api/prompts/optimize"""

    def test_unauthenticated(self):
        """No Bearer token → 401."""
        response = client.post("/api/prompts/optimize", json={"scene_text": "test"})
        assert response.status_code == 401

    @patch("routers.prompt.optimize_prompt_service", new_callable=AsyncMock)
    def test_success(self, mock_optimize: AsyncMock):
        """Valid token + valid text → 200 with prompts list."""
        from services.prompt_service import OptimizePromptResult
        mock_optimize.return_value = OptimizePromptResult(
            prompts=["a majestic mountain landscape at sunset, vibrant colors, cinematic lighting"]
        )
        token = _valid_token()
        response = client.post(
            "/api/prompts/optimize",
            json={"scene_text": "a mountain at sunset"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "prompts" in data
        assert isinstance(data["prompts"], list)
        assert len(data["prompts"]) == 1
        assert "cinematic lighting" in data["prompts"][0]

    @patch("routers.prompt.optimize_prompt_service", new_callable=AsyncMock)
    def test_success_with_segments(self, mock_optimize: AsyncMock):
        """Segments list → multiple prompts returned."""
        from services.prompt_service import OptimizePromptResult
        mock_optimize.return_value = OptimizePromptResult(
            prompts=["prompt for scene one", "prompt for scene two"]
        )
        token = _valid_token()
        response = client.post(
            "/api/prompts/optimize",
            json={
                "scene_text": "full scene text",
                "segments": ["scene one text", "scene two text"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["prompts"]) == 2

    def test_invalid_input_empty_text(self):
        """Empty scene_text → 422 validation error."""
        token = _valid_token()
        response = client.post(
            "/api/prompts/optimize",
            json={"scene_text": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_invalid_input_missing_field(self):
        """Missing scene_text → 422 validation error."""
        token = _valid_token()
        response = client.post(
            "/api/prompts/optimize",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422


# ── Classify Endpoint ────────────────────────────────────────────────────────


class TestClassifyEndpoint:
    """POST /api/prompts/classify"""

    def test_unauthenticated(self):
        """No Bearer token → 401."""
        response = client.post("/api/prompts/classify", json={"scene_text": "test"})
        assert response.status_code == 401

    @patch("routers.prompt._call_llm", new_callable=AsyncMock)
    def test_success(self, mock_call_llm: AsyncMock):
        """Valid token + valid text → 200 with scene_type & confidence."""
        mock_call_llm.return_value = "narrative"
        token = _valid_token()
        response = client.post(
            "/api/prompts/classify",
            json={"scene_text": "Once upon a time there was a brave knight"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["scene_type"] == "narrative"
        assert "confidence" in data
        assert isinstance(data["confidence"], (int, float))

    @patch("routers.prompt._call_llm", new_callable=AsyncMock)
    def test_descriptive_classification(self, mock_call_llm: AsyncMock):
        """Scene text with descriptive content → classified as descriptive."""
        mock_call_llm.return_value = "descriptive"
        token = _valid_token()
        response = client.post(
            "/api/prompts/classify",
            json={"scene_text": "The room was filled with antique furniture"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["scene_type"] == "descriptive"

    def test_invalid_input_empty_text(self):
        """Empty scene_text → 422 validation error."""
        token = _valid_token()
        response = client.post(
            "/api/prompts/classify",
            json={"scene_text": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_invalid_input_missing_field(self):
        """Missing scene_text → 422 validation error."""
        token = _valid_token()
        response = client.post(
            "/api/prompts/classify",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422
