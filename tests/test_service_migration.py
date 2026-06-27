"""Tests for service migration from settings to ProviderRouter.

TDD: RED phase — write failing tests first, then migrate services.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestRewriteMigration:
    """rewrite_content falls back to ProviderRouter."""
    pytestmark = pytest.mark.asyncio

    @patch("services.rewrite._call_llm", new_callable=AsyncMock)
    async def test_falls_back_to_router_when_api_key_none(self, mock_call_llm):
        """api_key=None → uses ProviderRouter to get openai config."""
        mock_call_llm.return_value = "rewritten content"

        mock_router = AsyncMock()
        mock_router.get.return_value = {
            "api_key": "router-key",
            "base_url": "https://api.openai.com/v1",
            "models": ["gpt-4o"],
        }

        with patch("services.rewrite.get_router", return_value=mock_router):
            from services.rewrite import rewrite_content
            result = await rewrite_content(
                content="Test content",
                api_key=None,
            )

        assert result.result_content == "rewritten content"
        mock_router.get.assert_called_once_with("openai")

    @patch("services.rewrite._call_llm", new_callable=AsyncMock)
    async def test_explicit_api_key_does_not_call_router(self, mock_call_llm):
        """api_key provided → doesn't call ProviderRouter."""
        mock_call_llm.return_value = "rewritten"

        with patch("services.rewrite.get_router") as mock_get_router:
            from services.rewrite import rewrite_content
            result = await rewrite_content(
                content="Test",
                api_key="explicit-key",
                base_url="https://api.openai.com/v1",
                model="gpt-4o",
            )

        mock_get_router.assert_not_called()
        assert result.result_content == "rewritten"

    async def test_no_key_no_router_raises_error(self):
        """No api_key and no router config → raises ValueError."""
        mock_router = AsyncMock()
        mock_router.get.return_value = None

        with patch("services.rewrite.get_router", return_value=mock_router):
            from services.rewrite import rewrite_content
            with pytest.raises(ValueError, match="API key"):
                await rewrite_content(
                    content="Test",
                    api_key=None,
                )


class TestTTSMigration:
    """TTS service falls back to ProviderRouter."""
    pytestmark = pytest.mark.asyncio

    @patch("services.tts_service.httpx.AsyncClient")
    async def test_text_to_speech_falls_back_to_router(self, mock_client_cls):
        """api_key=None → uses ProviderRouter to get doubao config."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {"code": 3000, "data": "AAAA"}

        mock_client.post.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {
            "api_key": "router-doubao-key",
            "base_url": "https://openspeech.bytedance.com/api/v1/tts",
            "models": [],
        }

        with patch("services.tts_service.get_router", return_value=mock_router):
            from services.tts_service import text_to_speech
            result = await text_to_speech(
                text="Hello",
                api_key=None,
            )

        assert result.error is None
        mock_router.get.assert_called_once_with("doubao")

    async def test_tts_no_key_no_router_returns_error(self):
        """No api_key and no router config → returns error in TTSResult."""
        mock_router = AsyncMock()
        mock_router.get.return_value = None

        with patch("services.tts_service.get_router", return_value=mock_router):
            from services.tts_service import text_to_speech
            result = await text_to_speech(
                text="Hello",
                api_key=None,
            )

        assert result.error is not None
        assert "API key" in result.error


class TestImageMigration:
    """Image service falls back to ProviderRouter."""
    pytestmark = pytest.mark.asyncio

    @patch("services.image_service.httpx.AsyncClient")
    async def test_minimax_falls_back_to_router(self, mock_client_cls):
        """api_key=None → miniMax uses ProviderRouter."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {
            "base_resp": {"status_code": 0},
            "data": {"image_urls": ["https://example.com/img.png"]},
        }
        mock_client.post.return_value = mock_response
        mock_get_resp = AsyncMock()
        mock_get_resp.content = b"fake-image-data"
        mock_client.get.return_value = mock_get_resp

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-minimax-key"}

        with patch("services.image_service.get_router", return_value=mock_router):
            from services.image_service import (
                GenerateImageRequest,
                ImageProvider,
                generate_image,
            )
            result = await generate_image(
                GenerateImageRequest(
                    prompt="Test image",
                    provider=ImageProvider.MINIMAX,
                    api_key=None,
                )
            )

        assert result.error is None
        assert result.status.value == "completed"
        mock_router.get.assert_called_once_with("minimax")

    @patch("services.image_service.httpx.AsyncClient")
    async def test_sensenova_falls_back_to_router(self, mock_client_cls):
        """api_key=None → SenseNova uses ProviderRouter."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {"data": [{"url": "https://example.com/img.png"}]}
        mock_client.post.return_value = mock_response
        mock_get_resp = AsyncMock()
        mock_get_resp.content = b"fake-image-data"
        mock_client.get.return_value = mock_get_resp

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-sensenova-key"}

        with patch("services.image_service.get_router", return_value=mock_router):
            from services.image_service import (
                GenerateImageRequest,
                ImageProvider,
                generate_image,
            )
            result = await generate_image(
                GenerateImageRequest(
                    prompt="Test",
                    provider=ImageProvider.SENSENOVA,
                    api_key=None,
                )
            )

        assert result.error is None
        mock_router.get.assert_called_once_with("sensenova")

    @patch("services.image_service.httpx.AsyncClient")
    async def test_kling_image_falls_back_to_router(self, mock_client_cls):
        """api_key=None → Kling image uses ProviderRouter."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {"data": {"task_id": "task-123"}}
        mock_client.post.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-kling-key"}

        with patch("services.image_service.get_router", return_value=mock_router):
            from services.image_service import (
                GenerateImageRequest,
                ImageProvider,
                generate_image,
            )
            result = await generate_image(
                GenerateImageRequest(
                    prompt="Test",
                    provider=ImageProvider.KLING,
                    api_key=None,
                )
            )

        assert result.error is None
        assert result.task_id == "task-123"
        mock_router.get.assert_called_once_with("kling")


class TestVideoMigration:
    """Video service falls back to ProviderRouter."""
    pytestmark = pytest.mark.asyncio

    @patch("services.video_service.httpx.AsyncClient")
    async def test_kling_video_generate_falls_back_to_router(self, mock_client_cls):
        """api_key=None → Kling video generation uses ProviderRouter."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {"data": {"task_id": "task-123"}}
        mock_client.post.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-kling-key"}

        with patch("services.video_service.get_router", return_value=mock_router):
            from services.video_service import (
                GenerateVideoRequest,
                VideoProvider,
                generate_video,
            )
            result = await generate_video(
                GenerateVideoRequest(
                    prompt="Test video",
                    provider=VideoProvider.KLING,
                    api_key=None,
                )
            )

        assert result.error is None
        assert result.task_id == "task-123"
        mock_router.get.assert_called_once_with("kling")

    @patch("services.video_service.httpx.AsyncClient")
    async def test_jimeng_video_generate_falls_back_to_router(self, mock_client_cls):
        """api_key=None → Jimeng video generation uses ProviderRouter."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {"id": "task-456"}
        mock_client.post.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-jimeng-key"}

        with patch("services.video_service.get_router", return_value=mock_router):
            from services.video_service import (
                GenerateVideoRequest,
                VideoProvider,
                generate_video,
            )
            result = await generate_video(
                GenerateVideoRequest(
                    prompt="Test",
                    provider=VideoProvider.JIMENG,
                    api_key=None,
                )
            )

        assert result.error is None
        assert result.task_id == "task-456"
        mock_router.get.assert_called_once_with("jimeng")

    @patch("services.video_service.httpx.AsyncClient")
    async def test_query_kling_status_falls_back_to_router(self, mock_client_cls):
        """api_key=None → query_video_status uses ProviderRouter for kling."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {
            "data": {
                "task_status": "succeed",
                "videos": [{"url": "https://example.com/v.mp4"}],
            }
        }
        mock_client.get.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-kling-key"}

        with patch("services.video_service.get_router", return_value=mock_router):
            from services.video_service import VideoProvider, query_video_status
            result = await query_video_status(
                task_id="task-123",
                provider=VideoProvider.KLING,
                api_key=None,
            )

        assert result.status.value == "completed"
        mock_router.get.assert_called_once_with("kling")

    @patch("services.video_service.httpx.AsyncClient")
    async def test_query_jimeng_status_falls_back_to_router(self, mock_client_cls):
        """api_key=None → query_video_status uses ProviderRouter for jimeng."""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = AsyncMock()
        mock_response.json = lambda: {
            "status": "completed",
            "results": [{"url": "https://example.com/v.mp4"}],
        }
        mock_client.get.return_value = mock_response

        mock_router = AsyncMock()
        mock_router.get.return_value = {"api_key": "router-jimeng-key"}

        with patch("services.video_service.get_router", return_value=mock_router):
            from services.video_service import VideoProvider, query_video_status
            result = await query_video_status(
                task_id="task-456",
                provider=VideoProvider.JIMENG,
                api_key=None,
            )

        assert result.status.value == "completed"
        mock_router.get.assert_called_once_with("jimeng")

    async def test_video_no_key_no_router_returns_failed(self):
        """No api_key and no router config → query_video_status returns FAILED."""
        mock_router = AsyncMock()
        mock_router.get.return_value = None

        with patch("services.video_service.get_router", return_value=mock_router):
            from services.video_service import VideoProvider, query_video_status
            result = await query_video_status(
                task_id="task-123",
                provider=VideoProvider.KLING,
                api_key=None,
            )

        assert result.status.value == "failed"
        assert "API key" in (result.error or "")


class TestPublishMigration:
    """Publish service gets wechat creds from ProviderRouter."""
    pytestmark = pytest.mark.asyncio

    @patch("services.publish_service.WechatPublisher")
    async def test_publish_gets_wechat_creds_from_router(self, mock_publisher_cls):
        """publish_to_wechat uses ProviderRouter for wechat appid/secret."""
        mock_publisher = MagicMock()
        mock_publisher.upload_image.return_value = "media-id"
        mock_publisher.publish.return_value = MagicMock(
            success=True,
            publish_id="pub-123",
            article_url="https://mp.weixin.qq.com/s/test",
        )
        mock_publisher_cls.return_value = mock_publisher

        mock_router = AsyncMock()
        mock_router.get.return_value = {
            "api_key": "test-appid",
            "config": {"appsecret": "test-secret"},
        }

        with patch("services.publish_service.get_router", return_value=mock_router):
            from services.publish_service import publish_to_wechat
            result = await publish_to_wechat(
                title="Test",
                content_html="<p>test</p>",
            )

        assert result.success
        mock_router.get.assert_called_once_with("wechat")
        mock_publisher_cls.assert_called_once_with(
            appid="test-appid", secret="test-secret"
        )

    async def test_publish_no_router_returns_error(self):
        """No wechat config in router → returns error in PublishServiceResult."""
        mock_router = AsyncMock()
        mock_router.get.return_value = None

        with patch("services.publish_service.get_router", return_value=mock_router):
            from services.publish_service import publish_to_wechat
            result = await publish_to_wechat(
                title="Test",
                content_html="<p>test</p>",
            )

        assert not result.success
        assert result.error is not None
