"""Douyin video publisher — cookie-based API integration.

Publishes videos to 抖音 via creator internal API:
  1. Get upload auth token
  2. Upload video file (multipart/form-data)
  3. Create archive (submit the post)

Auth: sid_tt + sessionid cookies stored in ProviderRouter.

⚠️  This uses reverse-engineered internal APIs (referenced from
    Multi-Publish Python backend + yixiaoer decompilation).
    These endpoints may change without notice.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

from services.provider_router import get_router

# ─── Constants ───────────────────────────────────────────────────────────────

DOUYIN_API = {
    "upload_auth": "https://creator.douyin.com/web/api/media/upload/auth/v5/",
    "create_post": "https://creator.douyin.com/web/api/media/aweme/create/",
    "post_video": "https://creator.douyin.com/web/api/media/aweme/post/",
    "user_info": "https://creator.douyin.com/web/api/media/user/info",
}

MAX_RETRIES = 3

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://creator.douyin.com/",
    "Origin": "https://creator.douyin.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ─── Result types ────────────────────────────────────────────────────────────

@dataclass
class DouyinPublishResult:
    success: bool
    publish_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    duration: float = 0.0


# ─── Cookie helpers ──────────────────────────────────────────────────────────

def _cookies_to_dict(cookies: list[dict]) -> dict[str, str]:
    """Convert a list of cookie dicts to a flat dict."""
    result = {}
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value:
            result[name] = value
    return result


def _extract_csrf(cookies: list[dict]) -> str:
    """Extract csrf_session_id for X-CSRFToken header."""
    for c in cookies:
        if c.get("name") in ("csrf_session_id", "csrf_session"):
            return c.get("value", "")
    return ""


async def _get_douyin_cookies() -> tuple[dict[str, str], str]:
    """Load Douyin cookies from ProviderRouter.

    Returns:
        (cookie_dict, csrf_token)

    Raises:
        ValueError: if no douyin provider is configured.
    """
    router = get_router()
    cfg = await router.get("douyin")
    if not cfg:
        raise ValueError(
            "抖音 provider not configured. "
            "Add 'douyin' provider via admin panel "
            "with sid_tt + sessionid stored in config.cookies."
        )
    cookies = cfg.get("config", {}).get("cookies", [])
    if not cookies:
        raw = cfg.get("api_key", "")
        if raw:
            # Support "sid_tt=xxx; sessionid=yyy" format
            cookies = [_parse_cookie_line(raw)]
    if not cookies:
        raise ValueError("抖音 cookies (sid_tt, sessionid) not configured.")

    cookie_dict = _cookies_to_dict(cookies)
    csrf = _extract_csrf(cookies)

    # Must have at least sid_tt or sessionid
    if not cookie_dict.get("sid_tt") and not cookie_dict.get("sessionid"):
        raise ValueError(
            "抖音 cookies must include 'sid_tt' and/or 'sessionid'."
        )

    return cookie_dict, csrf


def _parse_cookie_line(raw: str) -> dict:
    """Parse 'sid_tt=xxx; sessionid=yyy' into a cookie dict."""
    parts = raw.split(";")
    result = {"name": "", "value": ""}
    for part in parts:
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k in ("sid_tt", "sessionid", "csrf_session_id",
                     "bd_ticket_guard_client_data"):
                result = {"name": k, "value": v}
                break
    return result


# ─── Video publishing ────────────────────────────────────────────────────────


async def publish_video(
    title: str,
    video_path: str,
    cover_path: Optional[str] = None,
    desc: str = "",
    tags: Optional[list[str]] = None,
) -> DouyinPublishResult:
    """Publish a video to 抖音.

    Flow: upload auth → upload file → create post.

    Args:
        title: Video title.
        video_path: Local path to the video file (MP4).
        cover_path: Optional path to cover image.
        desc: Video description.
        tags: Up to 3 tags (抖音 API limit).

    Returns:
        DouyinPublishResult with video URL.
    """
    start = time.time()
    tags = tags or []

    # ── Load auth ──────────────────────────────────────────────────────
    try:
        cookie_dict, csrf_token = await _get_douyin_cookies()
    except ValueError as e:
        return DouyinPublishResult(success=False, error=str(e),
                                    duration=time.time() - start)

    if not os.path.exists(video_path):
        return DouyinPublishResult(
            success=False,
            error=f"Video file not found: {video_path}",
            duration=time.time() - start,
        )

    headers = dict(_DEFAULT_HEADERS)
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        headers=headers,
        follow_redirects=True,
        timeout=60.0,
    ) as client:

        # ── Step 1: Get upload auth ────────────────────────────────────
        logger.info("抖音: getting upload auth...")
        try:
            upload_token = await _get_upload_auth(client)
        except Exception as e:
            return DouyinPublishResult(
                success=False, error=f"Upload auth failed: {e}",
                duration=time.time() - start,
            )

        # ── Step 2: Upload video file ──────────────────────────────────
        logger.info(f"抖音: uploading video ({os.path.getsize(video_path)} bytes)...")
        try:
            video_id = await _upload_video(client, video_path, upload_token)
        except Exception as e:
            return DouyinPublishResult(
                success=False, error=f"Upload failed: {e}",
                duration=time.time() - start,
            )

        # ── Step 3: Create post ────────────────────────────────────────
        logger.info(f"抖音: creating post '{title}'...")
        try:
            result_id = await _create_post(
                client, title=title, desc=desc, tags=tags,
                video_id=video_id,
            )
        except Exception as e:
            return DouyinPublishResult(
                success=False, error=f"Post creation failed: {e}",
                duration=time.time() - start,
            )

        elapsed = time.time() - start
        url = f"https://www.douyin.com/video/{result_id}"
        logger.success(f"抖音: published {url} ({elapsed:.0f}s)")
        return DouyinPublishResult(
            success=True,
            publish_id=result_id,
            url=url,
            duration=elapsed,
        )


# ─── Step 1: Upload auth ─────────────────────────────────────────────────────


async def _get_upload_auth(client: httpx.AsyncClient) -> dict:
    """Get upload authorization token from Douyin."""
    resp = await client.get(DOUYIN_API["upload_auth"], timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Upload auth: HTTP {resp.status_code}")

    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"Upload auth rejected: {body.get('msg', str(body))}"
        )

    token = body.get("data", {})
    logger.info(f"Upload auth OK: {json.dumps(token, ensure_ascii=False)[:200]}")
    return token


# ─── Step 2: Upload video ────────────────────────────────────────────────────


async def _upload_video(
    client: httpx.AsyncClient,
    video_path: str,
    upload_token: dict,
) -> str:
    """Upload video file to Douyin via multipart/form-data.

    Returns:
        video_id string from upload response.
    """
    file_name = os.path.basename(video_path)

    with open(video_path, "rb") as f:
        file_data = f.read()

    upload_url = (
        upload_token.get("upload_url")
        or upload_token.get("video_url", "")
    )
    if not upload_url:
        raise RuntimeError("No upload_url in auth response")

    files = {"file": (file_name, file_data, "video/mp4")}

    resp = await client.post(
        upload_url,
        files=files,
        timeout=600.0,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed: HTTP {resp.status_code}")

    result = resp.json()
    video_id = (
        result.get("data", {}).get("video_id")
        or result.get("video_id")
        or result.get("data", {}).get("item_id", "")
    )
    if not video_id:
        logger.warning(f"No video_id in upload response: {resp.text[:200]}")
        video_id = f"auto_{int(time.time())}"

    logger.info(f"Upload complete: video_id={video_id}")
    return video_id


# ─── Step 3: Create post ─────────────────────────────────────────────────────


async def _create_post(
    client: httpx.AsyncClient,
    title: str,
    video_id: str,
    desc: str = "",
    tags: Optional[list[str]] = None,
) -> str:
    """Create a Douyin post with the uploaded video.

    Returns:
        item/video ID string.
    """
    tags = tags or []

    payload = {
        "title": title,
        "content": desc or title,
        "video_id": video_id,
        "tags": tags[:3],  # 抖音 API 限制最多 3 个标签
        "is_draft": False,
        "source": 1,  # web upload
    }

    # Try primary create API
    resp = await client.post(
        DOUYIN_API["create_post"],
        json=payload,
        timeout=30.0,
    )

    # Fallback to post_video API if primary fails
    if resp.status_code != 200:
        resp = await client.post(
            DOUYIN_API["post_video"],
            json=payload,
            timeout=30.0,
        )

    body = resp.json() if resp.text else {}
    if resp.status_code == 200 and body.get("code") == 0:
        item_id = (
            body.get("data", {}).get("item_id")
            or body.get("data", {}).get("video_id")
            or video_id
        )
        return str(item_id)

    raise RuntimeError(
        f"Create post failed: HTTP {resp.status_code}, "
        f"code={body.get('code')}, msg={body.get('msg', resp.text[:200])}"
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def verify_auth() -> dict:
    """Check if the stored Douyin cookies are still valid.

    Returns:
        dict with 'valid' (bool) and 'user_name' (str, if valid).
    """
    try:
        cookie_dict, _ = await _get_douyin_cookies()
    except ValueError:
        return {"valid": False, "user_name": None}

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        headers=_DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        resp = await client.get(DOUYIN_API["user_info"])
        if resp.status_code != 200:
            return {"valid": False, "user_name": None}
        data = resp.json()
        if data.get("code") == 0:
            info = data.get("data", {}).get("nickname", "")
            return {"valid": True, "user_name": info}
        return {"valid": False, "user_name": None}
