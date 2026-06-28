"""Bilibili video publisher — direct API integration.

Publishes videos to B站 via member API:
  1. Pre-upload (get upload URL and auth)
  2. Video file upload (chunked for large files)
  3. Create archive (submit the post)

Auth: SESSDATA + bili_jct cookies stored in ProviderRouter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from services.provider_router import get_router

# ─── Constants ───────────────────────────────────────────────────────────────

BILIBILI_API = {
    "pre_upload": "https://member.bilibili.com/preupload",
    "upload_chunk": "https://member.bilibili.com/x/web-interface/upload/chunk",
    "create_archive": "https://member.bilibili.com/x4/web-interface/archive/create",
    "user_info": "https://api.bilibili.com/x/web-interface/nav",
}

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per chunk
MAX_RETRIES = 3

# Default headers mimicking a recent Chrome on Windows
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://member.bilibili.com/",
    "Origin": "https://member.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ─── Result types ────────────────────────────────────────────────────────────

@dataclass
class BilibiliPublishResult:
    success: bool
    publish_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class _UploadToken:
    """Returned from pre-upload."""
    upload_url: str
    complete_url: str
    file_name: str
    biz_id: str
    version: str = "2"
    chunk_size: int = CHUNK_SIZE


# ─── Cookie helpers ──────────────────────────────────────────────────────────

def _cookies_to_header(cookies: list[dict]) -> str:
    """Convert a list of cookie dicts to a Cookie header string."""
    pairs = []
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value and name in ("SESSDATA", "bili_jct", "DedeUserID", "sid", "buvid3"):
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def _get_bilibili_cookies() -> tuple[list[dict], str]:
    """Load B cookies from ProviderRouter or directly from DB."""
    import aiosqlite

    cfg = None
    try:
        router = get_router()
        if router._db is not None:
            cfg = await router.get("bilibili")
    except Exception:
        pass

    if cfg is None:
        try:
            db = await aiosqlite.connect("orchestrator.db")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT config FROM provider_configs WHERE name = ?", ("bilibili",)
            )
            row = await cursor.fetchone()
            if row:
                config_data = json.loads(row["config"])
                cfg = {"config": config_data}
            await db.close()
        except Exception as e:
            raise ValueError(f"Failed to read bilibili cookies from DB: {e}")

    if not cfg:
        raise ValueError(
            "B provider not configured. "
            "Add bilibili provider with SESSDATA in config.cookies."
        )

    cookies = cfg.get("config", {}).get("cookies", [])
    if not cookies:
        raw = cfg.get("api_key", "")
        if raw:
            cookies = [_parse_cookie_line(raw)]
    if not cookies:
        raise ValueError("B SESSDATA not configured.")
    return cookies, _cookies_to_header(cookies)



def _parse_cookie_line(raw: str) -> dict:
    """Parse 'SESSDATA=xxx; bili_jct=yyy' into a cookie dict."""
    parts = raw.split(";")
    result = {"name": "", "value": ""}
    for part in parts:
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k in ("SESSDATA", "bili_jct", "DedeUserID"):
                result = {"name": k, "value": v}
                break
    return result


def _get_csrf(cookies: list[dict]) -> str:
    """Extract bili_jct value for CSRF token."""
    for c in cookies:
        if c.get("name") == "bili_jct":
            return c.get("value", "")
    return ""


# ─── Video publishing ────────────────────────────────────────────────────────


async def publish_video(
    title: str,
    video_path: str,
    cover_path: Optional[str] = None,
    desc: str = "",
    tags: Optional[list[str]] = None,
    source: str = "创作工具",
    tid: int = 128,  # Default: 128 (AI相关), full list in B站 API docs
    no_reprint: int = 1,
) -> BilibiliPublishResult:
    """Publish a video to B站.

    Flow: pre-upload → chunked upload → create archive.

    Args:
        title: Video title (1-80 chars).
        video_path: Local path to the video file (MP4).
        cover_path: Optional path to cover image.
        desc: Video description (0-1000 chars).
        tags: Up to 12 tags.
        source: Source attribution string.
        tid: B站 category ID (分区). 128 = AI/技术.
        no_reprint: 1 = disable reprint, 0 = allow.

    Returns:
        BilibiliPublishResult with video URL (bilibili.com/video/BVxxx).
    """
    start = time.time()
    tags = tags or []

    # ── Load auth ──────────────────────────────────────────────────────
    try:
        cookies, cookie_header = await _get_bilibili_cookies()
    except ValueError as e:
        return BilibiliPublishResult(success=False, error=str(e), duration=time.time() - start)

    if not os.path.exists(video_path):
        return BilibiliPublishResult(
            success=False,
            error=f"Video file not found: {video_path}",
            duration=time.time() - start,
        )

    csrf = _get_csrf(cookies)

    async with httpx.AsyncClient(
        headers={**_DEFAULT_HEADERS, "Cookie": cookie_header},
        follow_redirects=True,
        timeout=60.0,
    ) as client:

        # ── Step 1: Pre-upload ─────────────────────────────────────────
        logger.info("B站: pre-upload starting...")
        try:
            token = await _pre_upload(client, video_path)
        except Exception as e:
            return BilibiliPublishResult(
                success=False, error=f"Pre-upload failed: {e}", duration=time.time() - start
            )

        # ── Step 2: Upload file (chunked if large) ─────────────────────
        logger.info(f"B站: uploading {token.file_name} ({os.path.getsize(video_path)} bytes)...")
        try:
            file_size = os.path.getsize(video_path)
            if file_size > CHUNK_SIZE:
                await _upload_chunked(client, video_path, token)
            else:
                await _upload_single(client, video_path, token)
        except Exception as e:
            return BilibiliPublishResult(
                success=False, error=f"Upload failed: {e}", duration=time.time() - start
            )

        # ── Step 3: Create archive ─────────────────────────────────────
        logger.info(f"B站: creating archive '{title}'...")
        try:
            bv_id = await _create_archive(
                client, title=title, desc=desc, tags=tags,
                source=source, tid=tid, no_reprint=no_reprint,
                csrf=csrf,
            )
        except Exception as e:
            return BilibiliPublishResult(
                success=False, error=f"Archive creation failed: {e}", duration=time.time() - start
            )

        elapsed = time.time() - start
        url = f"https://www.bilibili.com/video/{bv_id}"
        logger.success(f"B站: published {url} ({elapsed:.0f}s)")
        return BilibiliPublishResult(
            success=True,
            publish_id=bv_id,
            url=url,
            duration=elapsed,
        )


# ─── Pre-upload ──────────────────────────────────────────────────────────────


async def _pre_upload(client: httpx.AsyncClient, video_path: str) -> _UploadToken:
    """Call B站 pre-upload to get upload URL and auth token."""
    file_name = os.path.basename(video_path)
    file_size = os.path.getsize(video_path)

    params = {
        "name": file_name,
        "size": file_size,
        "r": "upos",
        "profile": "ugcupos",
        "ssl": "0",
        "version": "2.0.0",
    }

    resp = await client.get(BILIBILI_API["pre_upload"], params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    if data.get("OK") != 1:
        raise RuntimeError(f"Pre-upload rejected: {data.get('msg', str(data))}")

    up_param = data.get("up_params", {}) or {}

    # Build upload URL from response
    endpoint = data.get("endpoint", "")
    bucket = data.get("bucket", "ugcupos")
    upload_url = f"https://{endpoint}/{bucket}/{up_param.get('prefix', '')}/{file_name}"

    complete_url = data.get("complete_url", "")
    biz_id = str(data.get("biz_id", ""))

    logger.info(f"Pre-upload OK: endpoint={endpoint}, biz_id={biz_id}")

    return _UploadToken(
        upload_url=upload_url,
        complete_url=complete_url,
        file_name=file_name,
        biz_id=biz_id,
    )


# ─── Upload (single / chunked) ──────────────────────────────────────────────


async def _upload_single(client: httpx.AsyncClient, video_path: str, token: _UploadToken):
    """Upload entire file in a single request."""
    with open(video_path, "rb") as f:
        file_data = f.read()

    headers = {
        "Content-Type": "application/octet-stream",
        "X-Upos-Biz-Id": token.biz_id,
    }
    resp = await client.put(
        token.upload_url,
        content=file_data,
        headers=headers,
        timeout=600.0,
    )
    resp.raise_for_status()
    logger.info(f"Single upload complete: {token.file_name}")


async def _upload_chunked(client: httpx.AsyncClient, video_path: str, token: _UploadToken):
    """Upload file in chunks, then signal completion.

    Uses B站's upos chunked upload protocol.
    """
    file_size = os.path.getsize(video_path)
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    file_hash = hashlib.md5()
    chunk_hashes: list[str] = []

    with open(video_path, "rb") as f:
        for chunk_index in range(total_chunks):
            chunk_data = f.read(CHUNK_SIZE)
            if not chunk_data:
                break

            chunk_md5 = hashlib.md5(chunk_data).hexdigest()
            file_hash.update(chunk_data)
            chunk_hashes.append(chunk_md5)

            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.put(
                        f"{token.upload_url}?chunk={chunk_index}&chunks={total_chunks}&filesize={len(chunk_data)}",
                        content=chunk_data,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "X-Upos-Biz-Id": token.biz_id,
                            "Content-MD5": chunk_md5,
                        },
                        timeout=120.0,
                    )
                    resp.raise_for_status()
                    logger.info(f"Chunk {chunk_index + 1}/{total_chunks} uploaded")
                    break
                except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                    if attempt == MAX_RETRIES - 1:
                        raise
                    logger.warning(f"Chunk {chunk_index} failed (attempt {attempt + 1}), retrying...")
                    await asyncio.sleep(2 ** attempt)

    # Signal upload complete
    complete_payload = {
        "biz_id": token.biz_id,
        "file_name": token.file_name,
        "file_size": file_size,
        "file_md5": file_hash.hexdigest(),
        "chunks": chunk_hashes,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total_chunks,
    }

    resp = await client.post(
        f"{token.upload_url}?complete=1",
        json=complete_payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    logger.info(f"Chunked upload complete: {total_chunks} chunks")


# ─── Create archive ──────────────────────────────────────────────────────────


async def _create_archive(
    client: httpx.AsyncClient,
    title: str,
    desc: str,
    tags: list[str],
    source: str,
    tid: int,
    no_reprint: int,
    csrf: str,
) -> str:
    """Submit the video post to B站 and return the BV id."""
    payload = {
        "title": title,
        "desc": desc or "",
        "tag": ";".join(tags[:12]) if tags else "AI",
        "tid": tid,
        "source": source,
        "no_reprint": no_reprint,
        "cover": "",  # B站 generates auto-cover or user uploads later
        "csrf": csrf,
    }

    # Add csrf_token for URL-encoded forms
    if csrf:
        payload["csrf_token"] = csrf

    resp = await client.post(
        BILIBILI_API["create_archive"],
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": client.headers.get("Cookie", ""),
        },
        timeout=30.0,
    )

    body = resp.json()

    if resp.status_code != 200 or body.get("code") != 0:
        msg = body.get("message", body.get("msg", f"HTTP {resp.status_code}"))
        raise RuntimeError(f"Archive create failed: {msg}")

    bv_id = body.get("data", {}).get("bvid", "")
    if not bv_id:
        raise RuntimeError(f"No bvid in response: {body}")

    return bv_id


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def verify_auth() -> dict:
    """Check if the stored B站 SESSDATA is still valid.

    Returns:
        dict with 'valid' (bool) and 'user_name' (str, if valid).
    """
    try:
        _, cookie_header = await _get_bilibili_cookies()
    except ValueError:
        return {"valid": False, "user_name": None}

    async with httpx.AsyncClient(
        headers={**_DEFAULT_HEADERS, "Cookie": cookie_header},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        resp = await client.get(BILIBILI_API["user_info"])
        if resp.status_code != 200:
            return {"valid": False, "user_name": None}
        data = resp.json()
        if data.get("code") == 0:
            info = data.get("data", {}).get("uname", "")
            return {"valid": True, "user_name": info}
        return {"valid": False, "user_name": None}
