"""Tests for video_service — query_video_status with real API polling.

TDD: RED phase — write failing tests first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.video_service import (
    VideoProvider,
    VideoStatus,
    _retry_with_backoff,
    query_video_status,
)

# ── _retry_with_backoff tests ────────────────────────────────────────────────


class TestRetryWithBackoff:
    """_retry_with_backoff: async retry helper with exponential backoff."""
    pytestmark = pytest.mark.asyncio

    async def test_success_first_try(self):
        """Function succeeds on first call → returns result immediately."""
        async def ok():
            return "done"

        result = await _retry_with_backoff(ok, max_retries=3)
        assert result == "done"

    async def test_retries_on_http_error_then_succeeds(self):
        """Function fails twice with HTTP error, succeeds on 3rd → returns result."""
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.HTTPStatusError(
                    "Server error",
                    request=httpx.Request("GET", "https://example.com"),
                    response=httpx.Response(500),
                )
            return "recovered"

        result = await _retry_with_backoff(flaky, max_retries=3)
        assert result == "recovered"
        assert call_count == 3

    async def test_all_retries_exhausted(self):
        """All retries fail → last exception propagates."""
        call_count = 0

        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError(
                "Server error",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(502),
            )

        with pytest.raises(httpx.HTTPStatusError):
            await _retry_with_backoff(always_fail, max_retries=3)
        assert call_count == 3

    async def test_connect_error_triggers_retry(self):
        """Connection error (ConnectError) also triggers retry."""
        call_count = 0

        async def conn_fail():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.ConnectError("Connection refused")
            return "connected"

        result = await _retry_with_backoff(conn_fail, max_retries=3)
        assert result == "connected"
        assert call_count == 2

    async def test_non_retryable_error_propagates_immediately(self):
        """Non-HTTP/connect errors (e.g. ValueError) propagate without retry."""
        call_count = 0

        async def bad():
            nonlocal call_count
            call_count += 1
            raise ValueError("Bad input")

        with pytest.raises(ValueError):
            await _retry_with_backoff(bad, max_retries=3)
        assert call_count == 1


# ── Kling status query tests ─────────────────────────────────────────────────


class TestKlingQueryStatus:
    """_query_kling_status: maps Kling API response to VideoResult."""
    pytestmark = pytest.mark.asyncio

    @patch("services.video_service.httpx.AsyncClient")
    async def test_pending_status(self, mock_client_cls):
        """Kling 'submitted' → PENDING status."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"data": {"task_status": "submitted"}},
        )

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.PENDING
        assert result.task_id == "task-123"
        assert result.provider == VideoProvider.KLING

    @patch("services.video_service.httpx.AsyncClient")
    async def test_processing_status(self, mock_client_cls):
        """Kling 'processing' → PROCESSING status."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"data": {"task_status": "processing"}},
        )

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.PROCESSING

    @patch("services.video_service.httpx.AsyncClient")
    async def test_completed_status(self, mock_client_cls):
        """Kling 'succeed' → COMPLETED status with video_url."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "task_status": "succeed",
                    "videos": [{"url": "https://example.com/video.mp4"}],
                }
            },
        )

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.COMPLETED
        assert result.video_url == "https://example.com/video.mp4"
        assert result.progress == 100

    @patch("services.video_service.httpx.AsyncClient")
    async def test_failed_status(self, mock_client_cls):
        """Kling 'failed' → FAILED status with error."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"data": {"task_status": "failed"}},
        )

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.FAILED

    @patch("services.video_service.httpx.AsyncClient")
    async def test_kling_api_called_with_correct_url(self, mock_client_cls):
        """Kling query hits the correct endpoint with Bearer token."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock(status_code=200)
        mock_response.json = lambda: {"data": {"task_status": "processing"}}
        mock_client.get.return_value = mock_response

        await query_video_status(
            task_id="task-kling-456",
            provider=VideoProvider.KLING,
            api_key="my-kling-key",
        )

        mock_client.get.assert_called_once()
        call_url = mock_client.get.call_args[0][0]
        assert "task-kling-456" in call_url
        assert "kling" in call_url.lower() or "kuaishou" in call_url.lower()

    @patch("services.video_service.httpx.AsyncClient")
    async def test_kling_missing_api_key(self, mock_client_cls):
        """No API key provided and no env var → FAILED with error."""
        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key=None,
        )

        assert result.status == VideoStatus.FAILED
        assert result.error is not None
        assert "API key" in result.error.lower() or "key" in result.error.lower()


# ── Jimeng status query tests ────────────────────────────────────────────────


class TestJimengQueryStatus:
    """_query_jimeng_status: maps Jimeng API response to VideoResult."""
    pytestmark = pytest.mark.asyncio

    @patch("services.video_service.httpx.AsyncClient")
    async def test_pending_status(self, mock_client_cls):
        """Jimeng 'pending' → PENDING status."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"status": "pending"},
        )

        result = await query_video_status(
            task_id="task-456",
            provider=VideoProvider.JIMENG,
            api_key="test-key",
        )

        assert result.status == VideoStatus.PENDING
        assert result.task_id == "task-456"
        assert result.provider == VideoProvider.JIMENG

    @patch("services.video_service.httpx.AsyncClient")
    async def test_processing_status(self, mock_client_cls):
        """Jimeng 'processing' → PROCESSING status."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"status": "processing"},
        )

        result = await query_video_status(
            task_id="task-456",
            provider=VideoProvider.JIMENG,
            api_key="test-key",
        )

        assert result.status == VideoStatus.PROCESSING

    @patch("services.video_service.httpx.AsyncClient")
    async def test_completed_status(self, mock_client_cls):
        """Jimeng 'completed' → COMPLETED status with video_url."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {
                "status": "completed",
                "results": [{"url": "https://example.com/jimeng-video.mp4"}],
            },
        )

        result = await query_video_status(
            task_id="task-456",
            provider=VideoProvider.JIMENG,
            api_key="test-key",
        )

        assert result.status == VideoStatus.COMPLETED
        assert result.video_url == "https://example.com/jimeng-video.mp4"
        assert result.progress == 100

    @patch("services.video_service.httpx.AsyncClient")
    async def test_failed_status(self, mock_client_cls):
        """Jimeng 'failed' → FAILED status with error."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {"status": "failed"},
        )

        result = await query_video_status(
            task_id="task-456",
            provider=VideoProvider.JIMENG,
            api_key="test-key",
        )

        assert result.status == VideoStatus.FAILED

    @patch("services.video_service.httpx.AsyncClient")
    async def test_jimeng_api_called_with_correct_url(self, mock_client_cls):
        """Jimeng query hits the correct endpoint with Bearer token."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock(status_code=200)
        mock_response.json = lambda: {"status": "processing"}
        mock_client.get.return_value = mock_response

        await query_video_status(
            task_id="task-jimeng-789",
            provider=VideoProvider.JIMENG,
            api_key="my-jimeng-key",
        )

        mock_client.get.assert_called_once()
        call_url = mock_client.get.call_args[0][0]
        assert "task-jimeng-789" in call_url

    @patch("services.video_service.httpx.AsyncClient")
    async def test_jimeng_missing_api_key(self, mock_client_cls):
        """No API key provided and no env var → FAILED with error."""
        result = await query_video_status(
            task_id="task-456",
            provider=VideoProvider.JIMENG,
            api_key=None,
        )

        assert result.status == VideoStatus.FAILED
        assert result.error is not None
        assert "API key" in result.error.lower() or "key" in result.error.lower()


# ─── Integration-style tests ──────────────────────────────────────────────────


class TestQueryVideoStatusIntegration:
    """query_video_status with retries and error handling."""
    pytestmark = pytest.mark.asyncio

    @patch("services.video_service.httpx.AsyncClient")
    async def test_retry_on_http_error_then_success(self, mock_client_cls):
        """query_video_status retries on HTTP 5xx, eventually succeeds."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # First call fails with 502, second succeeds
        mock_client.get.side_effect = [
            httpx.HTTPStatusError(
                "Bad gateway",
                request=httpx.Request("GET", "https://api.kling.kuaishou.com/v1/videos/text2video/task-123"),
                response=httpx.Response(502),
            ),
            AsyncMock(
                status_code=200,
                json=lambda: {"data": {"task_status": "succeed", "videos": [{"url": "https://example.com/v.mp4"}]}},
            ),
        ]

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.COMPLETED
        assert result.video_url == "https://example.com/v.mp4"
        assert mock_client.get.call_count == 2

    @patch("services.video_service.httpx.AsyncClient")
    async def test_all_retries_fail_returns_failed(self, mock_client_cls):
        """All retries exhausted → returns FAILED with error message."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "Service unavailable",
            request=httpx.Request("GET", "https://api.kling.kuaishou.com/v1/videos/text2video/task-123"),
            response=httpx.Response(503),
        )

        result = await query_video_status(
            task_id="task-123",
            provider=VideoProvider.KLING,
            api_key="test-key",
        )

        assert result.status == VideoStatus.FAILED
        assert result.error is not None
        # Should have attempted multiple retries
        assert mock_client.get.call_count == 3

    @patch("services.video_service.httpx.AsyncClient")
    async def test_unsupported_provider(self, mock_client_cls):
        """Provider not in supported list → FAILED with error."""
        result = await query_video_status(
            task_id="task-999",
            provider=VideoProvider.SORA,
            api_key="test-key",
        )

        assert result.status == VideoStatus.FAILED
        assert result.error is not None
        assert "unsupported" in result.error.lower() or "SORA" in result.error or "sora" in result.error.lower()
