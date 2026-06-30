"""Xiaohongshu (小红书) video publisher — COS upload + edith API.

Flow:
  1. Get upload permit (cookie-only) → COS credentials
  2. Get upload ID via COS Init
  3. Upload video in 5MB chunks via COS
  4. Complete multipart upload via COS
  5. Get cover upload permit → upload cover to COS
  6. Build post data JSON
  7. Generate X-s/X-t signature via Node.js (getSign$6)
  8. POST to edith.xiaohongshu.com/web_api/sns/v2/note

Auth: Cookies (a1, web_session) stored in ProviderRouter.
      X-s/X-t generated from the obfuscated getSign$6 JS function.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from services.provider_router import get_router

# ─── Constants ───────────────────────────────────────────────────────────────

CREATOR_API = "https://creator.xiaohongshu.com"
EDITH_API = "https://edith.xiaohongshu.com"

CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per COS part
MAX_RETRIES = 3
SIGN_SCRIPT = Path(__file__).parent / "xiaohongshu_sign.js"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 Edg/110.0.0.0"
    ),
    "Referer": "https://creator.xiaohongshu.com/publish/publish",
    "Origin": "https://creator.xiaohongshu.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ─── Result types ────────────────────────────────────────────────────────────


@dataclass
class XiaohongshuPublishResult:
    success: bool
    publish_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class _UploadPermit:
    """COS upload credentials from the permit API."""
    token: str
    fileIds: list[str]
    uploadAddr: str
    bucket: str = ""
    region: str = ""
    expireTime: int = 0


# ─── Cookie helpers ─────────────────────────────────────────────────────────


async def _get_xhs_cookies() -> list[dict]:
    """Load 小红书 cookies from ProviderRouter.

    Returns:
        List of cookie dicts.

    Raises:
        ValueError: if no xiaohongshu provider is configured.
    """
    router = get_router()
    cfg = await router.get("xiaohongshu")
    if not cfg:
        raise ValueError(
            "小红书 provider not configured. "
            "Push cookies via POST /api/jobs/cookies/xiaohongshu."
        )
    cookies = cfg.get("config", {}).get("cookies", [])
    if not cookies:
        raise ValueError("小红书 cookies not configured.")
    return cookies


def _cookies_to_header(cookies: list[dict]) -> str:
    """Convert cookie list to header string."""
    pairs = []
    for c in cookies:
        n, v = c.get("name", ""), c.get("value", "")
        if n and v:
            pairs.append(f"{n}={v}")
    return "; ".join(pairs)


def _cookies_to_dict(cookies: list[dict]) -> dict[str, str]:
    """Convert cookie list to flat dict."""
    out = {}
    for c in cookies:
        n, v = c.get("name", ""), c.get("value", "")
        if n and v:
            out[n] = v
    return out


# ─── Signing ─────────────────────────────────────────────────────────────────


def _generate_sign(url_path: str, body: Optional[dict] = None) -> dict:
    """Generate X-s/X-t signature via Node.js subprocess.

    Calls the extracted getSign$6 function from 蚁小二 decompiled code.

    Returns:
        dict with 'X-s', 'X-t', and optionally 'X-S-Common'.
    """
    if not SIGN_SCRIPT.exists():
        raise RuntimeError(
            f"Sign script not found: {SIGN_SCRIPT}. "
            "Reinstall orchestrator package."
        )

    args = ["node", str(SIGN_SCRIPT), url_path]
    if body is not None:
        args.append(json.dumps(body, ensure_ascii=False, separators=(",", ":")))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Sign script timed out (Node.js)")

    if result.returncode != 0:
        raise RuntimeError(
            f"Sign script failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    out = result.stdout.strip()
    if not out:
        raise RuntimeError(f"Sign script returned empty output. stderr={result.stderr.strip()}")

    try:
        sig = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Sign script output not JSON: {out[:200]} — {e}")

    if not sig.get("X-s") or not sig.get("X-t"):
        raise RuntimeError(f"Sign script missing X-s/X-t: {sig}")

    return sig


# ─── COS Upload helpers ─────────────────────────────────────────────────────


async def _get_upload_permit(
    client: httpx.AsyncClient,
    cookie_header: str,
    scene: str = "video",
) -> _UploadPermit:
    """Step 1 / 5: Get COS upload credentials.

    Cookie-only API, no X-s/X-t needed.
    """
    url = (
        f"{CREATOR_API}/api/media/v1/upload/web/permit"
        f"?biz_name=spectrum&scene={scene}&file_count=1&version=1&source=web"
    )
    headers = {
        "cookie": cookie_header,
        "Referer": "https://creator.xiaohongshu.com/publish/publish",
        "Authorization": "",
    }
    resp = await client.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Upload permit: HTTP {resp.status_code} — {resp.text[:200]}")

    body = resp.json()
    permits = (
        (body.get("data") or {}).get("uploadTempPermits") or []
    )
    if not permits:
        raise RuntimeError(f"No uploadTempPermits in permit response: {resp.text[:200]}")

    p = permits[0]
    permit = _UploadPermit(
        token=p.get("token", ""),
        fileIds=p.get("fileIds", []),
        uploadAddr=p.get("uploadAddr", ""),
        bucket=p.get("bucket", ""),
        region=p.get("region", ""),
        expireTime=p.get("expireTime", 0),
    )
    logger.info(f"Upload permit OK: scene={scene}, fileId={permit.fileIds[0] if permit.fileIds else 'none'}")
    return permit


async def _get_upload_id(
    client: httpx.AsyncClient,
    permit: _UploadPermit,
    content_type: str = "video/mp4",
) -> str:
    """Step 2: Initiate COS multipart upload — get UploadId.

    Returns:
        UploadId string from XML response.
    """
    url = f"https://{permit.uploadAddr}/{permit.fileIds[0]}?uploads"
    headers = {
        "x-cos-security-token": permit.token,
        "Content-Type": content_type,
        "Referer": "https://creator.xiaohongshu.com/",
        "Origin": "https://creator.xiaohongshu.com",
        "Authorization": "",
    }
    resp = await client.post(url, headers=headers, content=b"", timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"COS Init: HTTP {resp.status_code} — {resp.text[:200]}")

    text = resp.text
    if "<UploadId>" not in text:
        raise RuntimeError(f"COS Init: no UploadId in response: {text[:200]}")

    upload_id = text.split("<UploadId>")[1].split("<")[0].strip()
    logger.info(f"COS UploadId: {upload_id}")
    return upload_id


async def _upload_part(
    client: httpx.AsyncClient,
    permit: _UploadPermit,
    upload_id: str,
    part_number: int,
    chunk_data: bytes,
) -> str:
    """Step 3: Upload a single COS part.

    Returns:
        ETag string from response header.
    """
    url = (
        f"https://{permit.uploadAddr}/{permit.fileIds[0]}"
        f"?partNumber={part_number}&uploadId={upload_id}"
    )
    headers = {
        "x-cos-security-token": permit.token,
        "Referer": "https://creator.xiaohongshu.com/",
        "Origin": "https://creator.xiaohongshu.com",
        "Authorization": "",
    }
    resp = await client.put(url, headers=headers, content=chunk_data, timeout=300)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"COS Part {part_number}: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    etag = (resp.headers.get("etag") or "").strip()
    if not etag:
        raise RuntimeError(f"COS Part {part_number}: no ETag in response headers")
    return etag


async def _complete_multipart_upload(
    client: httpx.AsyncClient,
    permit: _UploadPermit,
    upload_id: str,
    parts: dict[int, str],
) -> str:
    """Step 4: Complete COS multipart upload.

    Sends XML with part list. Returns response text.
    """
    xml_parts = "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>"
        for n, e in sorted(parts.items())
    )
    xml_body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<CompleteMultipartUpload>{xml_parts}</CompleteMultipartUpload>"
    )
    body_md5 = hashlib.md5(xml_body.encode()).digest()
    md5_b64 = base64.b64encode(body_md5).decode()

    url = (
        f"https://{permit.uploadAddr}/{permit.fileIds[0]}"
        f"?uploadId={upload_id}"
    )
    headers = {
        "x-cos-security-token": permit.token,
        "Content-MD5": md5_b64,
        "Referer": "https://creator.xiaohongshu.com/",
        "Origin": "https://creator.xiaohongshu.com",
        "Authorization": "",
    }
    resp = await client.post(url, headers=headers, content=xml_body, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"COS Complete: HTTP {resp.status_code} — {resp.text[:200]}")

    logger.info(f"COS Complete OK: {resp.text[:100]}")
    return resp.text


async def _upload_cover(
    client: httpx.AsyncClient,
    permit: _UploadPermit,
    image_data: bytes,
) -> str:
    """Step 6: Upload cover image to COS.

    Returns:
        Preview URL (x-ros-preview-url header) or fileId-based URL.
    """
    url = f"https://{permit.uploadAddr}/{permit.fileIds[0]}"
    headers = {
        "x-cos-security-token": permit.token,
        "Content-Type": "",
        "Referer": "https://creator.xiaohongshu.com/",
        "Origin": "https://creator.xiaohongshu.com",
        "Authorization": "",
    }
    resp = await client.put(url, headers=headers, content=image_data, timeout=120)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Cover upload: HTTP {resp.status_code} — {resp.text[:200]}")

    preview_url = (resp.headers.get("x-ros-preview-url") or "").strip()
    if preview_url:
        logger.info(f"Cover upload OK: preview={preview_url[:60]}")
        return preview_url

    logger.warning(f"No x-ros-preview-url, using fileId-based URL")
    return f"https://{permit.uploadAddr}/{permit.fileIds[0]}"


# ─── Post data builder ──────────────────────────────────────────────────────


def _build_post_data(
    title: str,
    desc: str,
    video_file_id: str,
    cover_file_id: str,
    tags: Optional[list[str]] = None,
    video_width: int = 720,
    video_height: int = 1280,
    video_duration: float = 0.0,
) -> str:
    """Build the JSON body for the publish API.

    Matches the structure from 蚁小二's buildPostData$K.
    Returns JSON string.
    """
    # Business binds
    biz_binds = {
        "version": 1,
        "noteId": 0,
        "bizType": 13,
        "noteOrderBind": {},
        "notePostTiming": {},
        "groupBind": {},
        "noteCollectionBind": {"id": ""},
        "optionRelationList": [],
        "liveNoticeBind": {},
    }

    # Hash tags
    hash_tags = []
    if tags:
        for t in tags[:10]:
            hash_tags.append({
                "id": "",
                "name": t,
                "link": "",
                "type": "topic",
            })

    # Common fields
    common = {
        "type": "video",
        "title": title,
        "note_id": "",
        "desc": desc,
        "source": json.dumps({
            "type": "web",
            "ids": "",
            "extraInfo": '{"systemId":"web"}',
        }, ensure_ascii=False),
        "business_binds": json.dumps(biz_binds, ensure_ascii=False),
        "ats": [],
        "biz_relations": None,
        "hash_tag": hash_tags,
        "post_loc": None,
        "privacy_info": {
            "op_type": 1,
            "type": 0,  # 0=public
        },
    }

    # Cover info (nested in video_info)
    cover = {
        "height": 1920,
        "file_id": cover_file_id,
        "fileid": cover_file_id,
        "width": 1080,
        "frame": {
            "ts": 0,
            "is_user_select": False,
            "is_upload": False,
        },
    }

    # Composite metadata
    duration_ms = int(video_duration * 1000) if video_duration > 0 else 0

    video_info = {
        "file_id": video_file_id,
        "fileid": video_file_id,
        "format_width": video_width,
        "format_height": video_height,
        "composite_metadata": {
            "video": {
                "bitrate": 0,
                "colour_primaries": "BT.709",
                "duration": duration_ms,
                "format": "AVC",
                "frame_rate": 30,
                "height": video_height,
                "matrix_coefficients": "BT.709",
                "rotation": 0,
                "transfer_characteristics": "BT.709",
                "width": video_width,
            },
            "audio": {
                "bitrate": 0,
                "channels": 1,
                "duration": duration_ms,
                "format": "AAC",
                "sampling_rate": 0,
            },
        },
        "timelines": [],
        "cover": cover,
        "chapters": [],
        "chapter_sync_text": False,
        "segments": {
            "count": 1,
            "need_slice": False,
            "items": [],
        },
        "entrance": "web",
    }

    post = {
        "common": common,
        "image_info": None,
        "video_info": video_info,
    }

    return json.dumps(post, ensure_ascii=False, separators=(",", ":"))


# ─── ffprobe helpers ────────────────────────────────────────────────────────


def _probe_video(path: str) -> dict:
    """Get video dimensions and duration via ffprobe.

    Returns:
        {width, height, duration_secs}. Falls back to defaults on failure.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"width": 720, "height": 1280, "duration_secs": 10.0}

        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", 10))
        width, height = 720, 1280
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                w = stream.get("width", 720)
                h = stream.get("height", 1280)
                if w and h:
                    width, height = w, h
                break
        return {"width": width, "height": height, "duration_secs": duration}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {"width": 720, "height": 1280, "duration_secs": 10.0}


# ─── Main publish function ──────────────────────────────────────────────────


async def publish_video(
    title: str,
    video_path: str,
    cover_path: Optional[str] = None,
    desc: str = "",
    tags: Optional[list[str]] = None,
    scheduled_at: Optional[str] = None,
) -> XiaohongshuPublishResult:
    """Publish a video to 小红书.

    Flow:
      1. Get upload permit (cookie-only) → COS credentials
      2. COS Init → UploadId
      3. Upload video in 5MB chunks
      4. COS Complete → file uploaded
      5. Get cover permit → upload cover
      6. Build post data → sign → publish to edith API

    Args:
        title: Video title.
        video_path: Local path to MP4.
        cover_path: Optional cover image (jpg/png).
        desc: Video description (supports #tags and @mentions).
        tags: Up to 10 tags.
        scheduled_at: ISO 8601 scheduled time (not supported by 小红书 API yet).

    Returns:
        XiaohongshuPublishResult with note URL.
    """
    import base64

    start = time.time()
    tags = tags or []

    # ── Load auth ─────────────────────────────────────────────────------
    try:
        cookies = await _get_xhs_cookies()
    except ValueError as e:
        return XiaohongshuPublishResult(success=False, error=str(e), duration=time.time() - start)

    cookie_header = _cookies_to_header(cookies)
    cookie_dict = _cookies_to_dict(cookies)

    if not os.path.exists(video_path):
        return XiaohongshuPublishResult(
            success=False,
            error=f"Video file not found: {video_path}",
            duration=time.time() - start,
        )

    # Detect video dimensions
    probe = _probe_video(video_path)
    vid_w, vid_h = probe["width"], probe["height"]
    vid_dur = probe["duration_secs"]

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        headers=_DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=60.0,
    ) as client:

        # ── Step 1: Get video upload permit ────────────────────────────
        logger.info("小红书: getting video upload permit...")
        try:
            video_permit = await _get_upload_permit(client, cookie_header, scene="video")
        except Exception as e:
            return XiaohongshuPublishResult(
                success=False, error=f"Video permit failed: {e}",
                duration=time.time() - start,
            )

        # ── Step 2: COS Init → UploadId ───────────────────────────────
        logger.info("小红书: COS Init...")
        try:
            upload_id = await _get_upload_id(client, video_permit, content_type="video/mp4")
        except Exception as e:
            return XiaohongshuPublishResult(
                success=False, error=f"COS Init failed: {e}",
                duration=time.time() - start,
            )

        # ── Step 3: Upload video in 5MB chunks ────────────────────────
        file_size = os.path.getsize(video_path)
        total_parts = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        logger.info(f"小红书: uploading video ({file_size} bytes, {total_parts} parts)...")

        parts: dict[int, str] = {}
        try:
            with open(video_path, "rb") as f:
                for part_num in range(1, total_parts + 1):
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    for attempt in range(MAX_RETRIES):
                        try:
                            etag = await _upload_part(
                                client, video_permit, upload_id, part_num, chunk,
                            )
                            parts[part_num] = etag
                            logger.info(f"  Part {part_num}/{total_parts} OK (attempt {attempt + 1})")
                            break
                        except Exception as e:
                            if attempt == MAX_RETRIES - 1:
                                raise
                            wait = 2 ** attempt
                            logger.warning(f"  Part {part_num} failed (attempt {attempt + 1}), retry in {wait}s: {e}")
                            await asyncio.sleep(wait)
        except Exception as e:
            return XiaohongshuPublishResult(
                success=False, error=f"Video upload failed: {e}",
                duration=time.time() - start,
            )

        if len(parts) < total_parts:
            return XiaohongshuPublishResult(
                success=False,
                error=f"Upload incomplete: {len(parts)}/{total_parts} parts",
                duration=time.time() - start,
            )

        # ── Step 4: COS Complete ──────────────────────────────────────
        logger.info("小红书: COS Complete...")
        try:
            await _complete_multipart_upload(client, video_permit, upload_id, parts)
        except Exception as e:
            return XiaohongshuPublishResult(
                success=False, error=f"COS Complete failed: {e}",
                duration=time.time() - start,
            )

        video_file_id = video_permit.fileIds[0]

        # ── Step 5-6: Cover upload ─────────────────────────────────────
        cover_file_id = ""
        if cover_path and os.path.exists(cover_path):
            logger.info("小红书: uploading cover...")
            try:
                with open(cover_path, "rb") as f:
                    cover_data = f.read()
                cover_permit = await _get_upload_permit(client, cookie_header, scene="image")
                await _upload_cover(client, cover_permit, cover_data)
                cover_file_id = cover_permit.fileIds[0]
                logger.info(f"小红书: cover uploaded, fileId={cover_file_id}")
            except Exception as e:
                logger.warning(f"Cover upload failed (non-fatal): {e}")
                cover_file_id = ""

        # ── Step 7: Build post data ────────────────────────────────────
        logger.info("小红书: building post data...")
        post_body = _build_post_data(
            title=title,
            desc=desc or title,
            video_file_id=video_file_id,
            cover_file_id=cover_file_id or video_file_id,
            tags=tags,
            video_width=vid_w,
            video_height=vid_h,
            video_duration=vid_dur,
        )

        # ── Step 8: Generate X-s/X-t signature ─────────────────────────
        logger.info("小红书: generating X-s/X-t signature...")
        try:
            sig = _generate_sign("/web_api/sns/v2/note", json.loads(post_body))
        except Exception as e:
            return XiaohongshuPublishResult(
                success=False, error=f"Sign failed: {e}",
                duration=time.time() - start,
            )

        # ── Step 9: Publish to edith API ───────────────────────────────
        url = f"{EDITH_API}/web_api/sns/v2/note"
        headers = {
            "cookie": cookie_header,
            "Referer": "https://creator.xiaohongshu.com/",
            "Origin": "https://creator.xiaohongshu.com",
            "Authorization": "",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
            "X-s": sig.get("X-s", ""),
            "X-t": sig.get("X-t", ""),
            "X-S-Common": sig.get("X-S-Common", ""),
        }

        logger.info("小红书: posting to edith API...")
        try:
            resp = await client.post(url, headers=headers, content=post_body, timeout=60)
        except httpx.TimeoutException:
            return XiaohongshuPublishResult(
                success=False, error="Publish API timeout",
                duration=time.time() - start,
            )
        except httpx.HTTPStatusError as e:
            return XiaohongshuPublishResult(
                success=False, error=f"Publish API HTTP {e.response.status_code}: {e.response.text[:200]}",
                duration=time.time() - start,
            )

        elapsed = time.time() - start

        # Parse response
        try:
            result = resp.json()
        except json.JSONDecodeError:
            return XiaohongshuPublishResult(
                success=False, error=f"Publish API non-JSON response: {resp.text[:200]}",
                duration=elapsed,
            )

        code = result.get("result", result.get("code", -1))
        msg = result.get("msg", result.get("message", ""))
        note_id = ""
        if result.get("data"):
            note_id = (result["data"].get("id") or "")

        if code == 0 and note_id:
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            logger.success(f"小红书: published {note_url} ({elapsed:.0f}s)")
            return XiaohongshuPublishResult(
                success=True,
                publish_id=note_id,
                url=note_url,
                duration=elapsed,
            )

        if "canvas illegal" in (msg or ""):
            error_msg = (
                "提示：请先在创作者中心发布一篇内容后再使用一键发布。"
            )
        else:
            error_msg = f"Publish failed: {msg or resp.text[:200]}"

        return XiaohongshuPublishResult(
            success=False, error=error_msg, duration=elapsed,
        )


# ─── Auth verification ──────────────────────────────────────────────────────


async def verify_auth() -> dict:
    """Check stored 小红书 cookies validity.

    Returns:
        dict with 'valid' (bool) and 'user_name' (str, if valid).
    """
    try:
        cookies = await _get_xhs_cookies()
    except ValueError:
        return {"valid": False, "user_name": None}

    cookie_header = _cookies_to_header(cookies)
    cookie_dict = _cookies_to_dict(cookies)

    headers = {
        "cookie": cookie_header,
        "Referer": "https://creator.xiaohongshu.com/creator/home",
        "Authorization": "",
    }

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        follow_redirects=True,
        timeout=15,
    ) as client:
        resp = await client.get(
            f"{CREATOR_API}/api/galaxy/user/info",
            headers=headers,
        )
        if resp.status_code != 200:
            return {"valid": False, "user_name": None}
        data = resp.json()
        user_info = data.get("data") or {}
        uname = user_info.get("userName") or user_info.get("nickname") or ""
        return {"valid": bool(uname), "user_name": uname}

