"""Tests for bilibili_publisher.py and douyin_publisher.py.

Covers:
- Cookie parsing helpers (no HTTP)
- Early error paths (missing cookies, missing file)
- verify_auth flow
- Pre-upload response parsing (with mocked HTTP)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services import bilibili_publisher as bilibili
from services import douyin_publisher as douyin

# ========================
# bilibili_publisher tests
# ========================

class TestBilibiliCookieHelpers:
    def test_cookies_to_header_filters_relevant(self):
        cookies = [{"name": "SESSDATA", "value": "abc123"}, {"name": "bili_jct", "value": "def456"}, {"name": "DedeUserID", "value": "789"}, {"name": "irrelevant", "value": "skip"}]
        result = bilibili._cookies_to_header(cookies)
        assert "SESSDATA=abc123" in result
        assert "bili_jct=def456" in result
        assert "irrelevant=skip" not in result

    def test_cookies_to_header_empty(self):
        assert bilibili._cookies_to_header([]) == ""

    def test_get_csrf_found(self):
        assert bilibili._get_csrf([{"name": "SESSDATA", "value": "abc"}, {"name": "bili_jct", "value": "csrf_token_value"}]) == "csrf_token_value"

    def test_get_csrf_not_found(self):
        assert bilibili._get_csrf([{"name": "foo", "value": "bar"}]) == ""

    def test_parse_cookie_line_sessdata(self):
        result = bilibili._parse_cookie_line("SESSDATA=abc123; bili_jct=def456")
        assert result["name"] == "SESSDATA"
        assert result["value"] == "abc123"

    def test_parse_cookie_line_no_match(self):
        result = bilibili._parse_cookie_line("foo=bar; baz=qux")
        assert result["name"] == ""
        assert result["value"] == ""


class TestBilibiliPublishErrors:
    @pytest.mark.asyncio
    async def test_publish_no_cookies(self):
        with patch.object(bilibili, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = None
            mock_get.return_value = router
            result = await bilibili.publish_video(title="Test", video_path="/nonexistent/video.mp4")
        assert result.success is False
        assert "not configured" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_publish_video_not_found(self):
        with patch.object(bilibili, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = {"config": {"cookies": [{"name": "SESSDATA", "value": "abc"}]}}
            mock_get.return_value = router
            result = await bilibili.publish_video(title="Test", video_path="/tmp/_nonexistent_video_xyz.mp4")
        assert result.success is False
        assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_verify_auth_no_cookies(self):
        with patch.object(bilibili, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = None
            mock_get.return_value = router
            result = await bilibili.verify_auth()
        assert result["valid"] is False


class TestBilibiliPreUpload:
    @pytest.mark.asyncio
    async def test_pre_upload_success(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_text("fake video content")
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"OK": 1, "endpoint": "upos-sz-upcdn.bilivideo.com", "bucket": "ugcupos", "biz_id": 12345, "up_params": {"prefix": "ugc/abc123"}, "complete_url": "https://member.bilibili.com/complete"}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        token = await bilibili._pre_upload(mock_client, str(video_file))
        assert token.biz_id == "12345"
        assert "upos-sz-upcdn.bilivideo.com" in token.upload_url
        assert "ugc/abc123" in token.upload_url

    @pytest.mark.asyncio
    async def test_pre_upload_rejected(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_text("fake")
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"OK": 0, "msg": "auth failed"}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        with pytest.raises(RuntimeError, match="auth failed"):
            await bilibili._pre_upload(mock_client, str(video_file))


class TestBilibiliCreateArchive:
    @pytest.mark.asyncio
    async def test_create_archive_success(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "data": {"bvid": "BV1GJ411x8x"}}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.headers = {"Cookie": "SESSDATA=abc"}
        bv_id = await bilibili._create_archive(mock_client, title="Test", desc="Desc", tags=["tag1"], source="test", tid=128, no_reprint=1, csrf="csrf_val")
        assert bv_id == "BV1GJ411x8x"

    @pytest.mark.asyncio
    async def test_create_archive_api_error(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": -400, "message": "title too long"}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.headers = {"Cookie": "SESSDATA=abc"}
        with pytest.raises(RuntimeError, match="title too long"):
            await bilibili._create_archive(mock_client, title="Test", desc="", tags=[], source="test", tid=128, no_reprint=1, csrf="csrf_val")

    @pytest.mark.asyncio
    async def test_create_archive_no_bvid(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "data": {}}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.headers = {"Cookie": "SESSDATA=abc"}
        with pytest.raises(RuntimeError, match="No bvid"):
            await bilibili._create_archive(mock_client, title="Test", desc="", tags=[], source="test", tid=128, no_reprint=1, csrf="csrf_val")


# ========================
# douyin_publisher tests
# ========================

class TestDouyinCookieHelpers:
    def test_cookies_to_dict(self):
        cookies = [{"name": "sid_tt", "value": "sid123"}, {"name": "sessionid", "value": "sess456"}, {"domain": ".douyin.com", "name": "csrf_session_id", "value": "csrf789"}]
        result = douyin._cookies_to_dict(cookies)
        assert result["sid_tt"] == "sid123"
        assert result["sessionid"] == "sess456"
        assert result["csrf_session_id"] == "csrf789"

    def test_cookies_to_dict_empty(self):
        assert douyin._cookies_to_dict([]) == {}

    def test_extract_csrf_found(self):
        assert douyin._extract_csrf([{"name": "csrf_session_id", "value": "csrf_val"}]) == "csrf_val"

    def test_extract_csrf_not_found(self):
        assert douyin._extract_csrf([{"name": "foo", "value": "bar"}]) == ""

    def test_parse_cookie_line_sid_tt(self):
        result = douyin._parse_cookie_line("sid_tt=abc123; sessionid=def456")
        assert result["name"] == "sid_tt"
        assert result["value"] == "abc123"

    def test_parse_cookie_line_sessionid(self):
        result = douyin._parse_cookie_line("sessionid=def456; sid_tt=abc123")
        assert result["name"] == "sessionid"
        assert result["value"] == "def456"

    def test_parse_cookie_line_no_match(self):
        result = douyin._parse_cookie_line("foo=bar")
        assert result["name"] == ""


class TestDouyinPublishErrors:
    @pytest.mark.asyncio
    async def test_publish_no_cookies(self):
        with patch.object(douyin, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = None
            mock_get.return_value = router
            result = await douyin.publish_video(title="Test", video_path="/nonexistent/video.mp4")
        assert result.success is False
        assert "not configured" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_publish_no_sid_tt(self):
        with patch.object(douyin, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = {"config": {"cookies": [{"name": "irrelevant", "value": "x"}]}}
            mock_get.return_value = router
            result = await douyin.publish_video(title="Test", video_path="/tmp/_nonexistent_test_video.mp4")
        assert result.success is False
        assert "sid_tt" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_publish_video_not_found(self):
        with patch.object(douyin, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = {"config": {"cookies": [{"name": "sid_tt", "value": "sid123"}, {"name": "sessionid", "value": "sess456"}]}}
            mock_get.return_value = router
            result = await douyin.publish_video(title="Test", video_path="/tmp/_nonexistent_video_xyz.mp4")
        assert result.success is False
        assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_verify_auth_no_cookies(self):
        with patch.object(douyin, "get_router") as mock_get:
            router = AsyncMock()
            router.get.return_value = None
            mock_get.return_value = router
            result = await douyin.verify_auth()
        assert result["valid"] is False


class TestDouyinUploadAuth:
    @pytest.mark.asyncio
    async def test_upload_auth_success(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "data": {"upload_url": "https://upload.douyin.com/video/upload", "video_url": ""}}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        token = await douyin._get_upload_auth(mock_client)
        assert token["upload_url"] == "https://upload.douyin.com/video/upload"

    @pytest.mark.asyncio
    async def test_upload_auth_rejected(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": -1, "msg": "cookie expired"}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        with pytest.raises(RuntimeError, match="cookie expired"):
            await douyin._get_upload_auth(mock_client)

    @pytest.mark.asyncio
    async def test_upload_auth_http_error(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        with pytest.raises(RuntimeError, match="HTTP 403"):
            await douyin._get_upload_auth(mock_client)


class TestDouyinCreatePost:
    @pytest.mark.asyncio
    async def test_create_post_success(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.text = '{"code": 0, "data": {"item_id": "123456789"}}'
        mock_resp.json.return_value = {"code": 0, "data": {"item_id": "123456789"}}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        item_id = await douyin._create_post(mock_client, title="Test", video_id="vid456", desc="Desc", tags=["tag1"])
        assert item_id == "123456789"

    @pytest.mark.asyncio
    async def test_create_post_api_error(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.text = '{"code": 10001, "msg": "video not ready"}'
        mock_resp.json.return_value = {"code": 10001, "msg": "video not ready"}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="video not ready"):
            await douyin._create_post(mock_client, title="Test", video_id="vid456")

    @pytest.mark.asyncio
    async def test_create_post_http_error(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await douyin._create_post(mock_client, title="Test", video_id="vid456")
