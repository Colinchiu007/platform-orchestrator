"""Tencent Video (微信视频号 / WeChat Channels) video publisher.

Publishes videos to 视频号 via channels.weixin.qq.com internal API:
  1. auth_data — get finder user info (uin, finderUsername)
  2. helper_upload_params — get upload auth key
  3. applyuploaddfs — init chunked upload session
  4. uploadpartdfs — upload video chunks (8 MB)
  5. completepartuploaddfs — finalize upload
  6. Upload cover image (if provided)
  7. post_create — submit the video post

Auth: Cookie from channels.weixin.qq.com domain, stored in ProviderRouter.

Uses reverse-engineered internal APIs (referenced from
yixiaoer decompilation). These endpoints may change without notice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from services.provider_router import get_router

# ─── Constants ───────────────────────────────────────────────────────────────

API = {
    "auth_data": "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data",
    "helper_upload_params": "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params",
    "post_create": "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create",
}

UPLOAD_HOST = "https://finderassistancea.video.qq.com"

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per chunk
MAX_RETRIES = 3

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://channels.weixin.qq.com/platform/post/create",
    "Origin": "https://channels.weixin.qq.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ─── Result types ────────────────────────────────────────────────────────────

@dataclass
class TencentPublishResult:
    success: bool
    publish_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    duration: float = 0.0


# ─── Cookie helpers ──────────────────────────────────────────────────────────

def _cookie_header(cookies: list[dict]) -> str:
    """Build Cookie header string from provider config cookies."""
    return "; ".join(
        f"{c['name']}={c['value']}"
        for c in cookies
        if c.get("name") and c.get("value")
    )


def _timestamp() -> int:
    return int(time.time() * 1000)


def _make_xargs(
    filetype: str,
    weixinnum: str,
    filekey: str,
    filesize: int,
    taskid: str,
    scene: int = 2,
) -> str:
    return (
        f"apptype=251&filetype={filetype}"
        f"&weixinnum={weixinnum}"
        f"&filekey={filekey}"
        f"&filesize={filesize}"
        f"&taskid={taskid}"
        f"&scene={scene}"
    )


# ─── Main publish function ──────────────────────────────────────────────────

async def publish_video(
    title: str,
    video_path: str,
    cover_path: Optional[str] = None,
    desc: str = "",
    tags: Optional[list[str]] = None,
    scheduled_at: Optional[str] = None,
) -> TencentPublishResult:
    """Publish a video to 微信视频号.

    Steps:
      1. Get cookies from ProviderRouter
      2. auth_data -> finderUsername / uin
      3. helper_upload_params -> authKey
      4. Init chunked upload -> UploadID
      5. Upload chunks
      6. Complete upload
      7. Upload cover (if provided)
      8. post_create -> submit
    """
    tags = tags or []
    start_time = time.time()

    # ── 1. Get cookies ────────────────────────────────────────────────────
    router = get_router()
    cfg = await router.get("tencent_video")
    if not cfg or not cfg.get("config", {}).get("cookies"):
        return TencentPublishResult(
            success=False,
            error="tencent_video cookies not configured. Push cookies via POST /api/jobs/cookies/tencent_video",
        )

    cookies_raw = cfg["config"]["cookies"]
    cookie_str = _cookie_header(cookies_raw)
    if not cookie_str:
        return TencentPublishResult(
            success=False, error="Empty cookies for tencent_video"
        )

    logger.info("[tencent_video] Starting publish: title={}, file={}", title, video_path)

    # ── 2. auth_data ──────────────────────────────────────────────────────
    finder_username, finder_uin = await _get_finder_info(cookie_str)
    if not finder_username:
        return TencentPublishResult(
            success=False, error="auth_data failed: no finderUsername (cookies expired?)"
        )

    # ── 3. helper_upload_params -> authKey ─────────────────────────────────
    auth_key, upload_uin = await _get_upload_auth(cookie_str, finder_username)
    if not auth_key:
        return TencentPublishResult(
            success=False, error="helper_upload_params failed: no authKey"
        )
    uin = upload_uin or finder_uin or finder_username

    # ── 4-6. Upload video ─────────────────────────────────────────────────
    filename = Path(video_path).name
    file_ext = Path(video_path).suffix.lstrip(".") or "mp4"
    file_size = os.path.getsize(video_path)
    task_id = str(uuid.uuid4()).replace("-", "")

    upload_id = await _init_upload(
        cookie_str, file_size, filename, file_ext, uin, task_id, auth_key
    )
    if not upload_id:
        return TencentPublishResult(
            success=False, error="Failed to init video upload"
        )

    success = await _perform_chunked_upload(
        cookie_str, video_path, file_ext, uin, task_id, auth_key, upload_id,
        file_size, filename,
    )
    if not success:
        return TencentPublishResult(
            success=False, error="Video chunk upload failed"
        )

    # ── 7. Upload cover ────────────────────────────────────────────────────
    cover_upload_id = None
    if cover_path and os.path.exists(cover_path):
        cover_size = os.path.getsize(cover_path)
        if cover_size > 512 * 1024:
            logger.warning("[tencent_video] Cover too large (>512KB), skipping")
        else:
            cover_ext = Path(cover_path).suffix.lstrip(".") or "jpg"
            cover_filename = Path(cover_path).name
            cover_task_id = str(uuid.uuid4()).replace("-", "")
            cover_upload_id = await _init_upload(
                cookie_str, cover_size, cover_filename, cover_ext,
                uin, cover_task_id, auth_key, scene=0,
            )
            if cover_upload_id:
                await _perform_chunked_upload(
                    cookie_str, cover_path, cover_ext, uin, cover_task_id, auth_key,
                    cover_upload_id, cover_size, cover_filename, scene=0,
                )

    # ── 8. post_create ─────────────────────────────────────────────────────
    result = await _create_post(
        cookie_str=cookie_str,
        title=title,
        desc=desc,
        tags=tags,
        finder_username=finder_username,
        uin=uin,
        video_path=video_path,
        upload_id=upload_id,
        file_ext=file_ext,
        file_size=file_size,
        auth_key=auth_key,
        task_id=task_id,
        cover_upload_id=cover_upload_id,
        scheduled_at=scheduled_at,
    )

    duration = time.time() - start_time
    result.duration = duration
    return result


# ─── Step implementations ──────────────────────────────────────────────────

async def _get_finder_info(cookie_str: str) -> tuple[Optional[str], Optional[str]]:
    """Step 2: Call auth_data to get finderUsername and uin."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        body = _auth_data_body()
        resp = await client.post(
            API["auth_data"],
            json=body,
            headers={**_DEFAULT_HEADERS, "cookie": cookie_str,
                     "Referer": "https://channels.weixin.qq.com",
                     "Origin": "https://channels.weixin.qq.com"},
        )
        data = resp.json()

    if not isinstance(data, dict):
        return None, None

    inner = data.get("data", {})
    finder_user = inner.get("finderUser", {})
    finder_username = finder_user.get("finderUsername")
    finder_uin = finder_user.get("uin")

    if not finder_username:
        err_code = data.get("errCode") or inner.get("errCode") or ""
        err_msg = data.get("errMsg") or inner.get("errMsg") or ""
        if "login" in err_msg.lower() or "auth" in err_msg.lower() or err_code in ("300333", "300334"):
            return None, None
    return finder_username, finder_uin


async def _get_upload_auth(
    cookie_str: str, finder_username: str
) -> tuple[Optional[str], Optional[str]]:
    """Step 3: Call helper_upload_params to get authKey and uin."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        body = {
            "timestamp": _timestamp(),
            "_log_finder_id": finder_username,
            "rawKeyBuff": None,
        }
        resp = await client.post(
            API["helper_upload_params"],
            json=body,
            headers={**_DEFAULT_HEADERS, "cookie": cookie_str,
                     "Referer": "https://channels.weixin.qq.com",
                     "Origin": "https://channels.weixin.qq.com"},
        )
        data = resp.json()

    if not isinstance(data, dict):
        return None, None

    inner = data.get("data", {})
    auth_key = inner.get("authKey")
    uin = inner.get("uin")
    return auth_key, uin


async def _init_upload(
    cookie_str: str,
    file_size: int,
    filename: str,
    file_ext: str,
    uin: str,
    task_id: str,
    auth_key: str,
    scene: int = 2,
) -> Optional[str]:
    """Step 4: Init chunked upload via applyuploaddfs.

    Returns UploadID on success, None on failure.
    """
    num_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
    chunk_sizes = _compute_chunk_sizes(file_size, num_chunks)

    body = {
        "BlockSum": num_chunks,
        "BlockPartLength": chunk_sizes,
    }

    filekey = filename
    xargs = _make_xargs(file_ext, uin, filekey, file_size, task_id, scene)

    headers = {
        **_DEFAULT_HEADERS,
        "cookie": cookie_str,
        "X-Arguments": xargs,
        "Authorization": auth_key,
        "Content-MD5": "null",
        "Referer": "https://channels.weixin.qq.com/",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.put(
            f"{UPLOAD_HOST}/applyuploaddfs",
            json=body,
            headers=headers,
        )
        data = resp.json()

    if isinstance(data, dict) and data.get("UploadID"):
        return data["UploadID"]

    logger.error("[tencent_video] init_upload failed: {}", data)
    return None


async def _perform_chunked_upload(
    cookie_str: str,
    file_path: str,
    file_ext: str,
    uin: str,
    task_id: str,
    auth_key: str,
    upload_id: str,
    file_size: int,
    filename: str,
    scene: int = 2,
) -> bool:
    """Step 5-6: Upload file chunks and complete the upload."""
    num_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))

    with open(file_path, "rb") as f:
        for chunk_idx in range(num_chunks):
            offset = chunk_idx * CHUNK_SIZE
            chunk_data = f.read(CHUNK_SIZE)
            part_number = chunk_idx + 1

            xargs = _make_xargs(file_ext, uin, filename, file_size, task_id, scene)
            chunk_md5 = hashlib.md5(chunk_data).hexdigest()

            headers = {
                **_DEFAULT_HEADERS,
                "cookie": cookie_str,
                "X-Arguments": xargs,
                "Authorization": auth_key,
                "Content-Type": "application/octet-stream",
                "Content-MD5": chunk_md5,
                "Referer": "https://channels.weixin.qq.com/platform/post/create",
            }

            url = f"{UPLOAD_HOST}/uploadpartdfs?PartNumber={part_number}&UploadID={upload_id}"

            success = False
            for attempt in range(MAX_RETRIES):
                try:
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        resp = await client.put(url, content=chunk_data, headers=headers)
                    if resp.status_code == 200:
                        success = True
                        break
                    logger.warning(
                        "[tencent_video] chunk {} attempt {} status={}",
                        part_number, attempt + 1, resp.status_code,
                    )
                except Exception as e:
                    logger.warning(
                        "[tencent_video] chunk {} attempt {} error: {}",
                        part_number, attempt + 1, e,
                    )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

            if not success:
                return False

    # ── Complete upload ───────────────────────────────────────────────────
    return await _complete_upload(
        cookie_str, upload_id, auth_key, file_ext, uin, file_size, filename, task_id, scene,
    )


async def _complete_upload(
    cookie_str: str,
    upload_id: str,
    auth_key: str,
    file_ext: str,
    uin: str,
    file_size: int,
    filename: str,
    task_id: str,
    scene: int = 2,
) -> bool:
    """Step 6: Finalize upload via completepartuploaddfs."""
    num_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
    chunk_sizes = _compute_chunk_sizes(file_size, num_chunks)

    part_info = []
    for i in range(num_chunks):
        part_info.append({
            "PartNumber": i + 1,
            "PartSize": chunk_sizes[i],
        })

    xargs = _make_xargs(file_ext, uin, filename, file_size, task_id, scene)
    body = {"TransFlag": "0_0", "PartInfo": part_info}

    headers = {
        **_DEFAULT_HEADERS,
        "cookie": cookie_str,
        "X-Arguments": xargs,
        "Authorization": auth_key,
        "Referer": "https://channels.weixin.qq.com/platform/post/create",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{UPLOAD_HOST}/completepartuploaddfs?UploadID={upload_id}",
            json=body,
            headers=headers,
        )
        result = resp.json()

    if isinstance(result, dict) and result.get("Result") == 0:
        return True

    logger.error("[tencent_video] complete_upload failed: {}", result)
    return False


async def _create_post(
    cookie_str: str,
    title: str,
    desc: str,
    tags: list[str],
    finder_username: str,
    uin: str,
    video_path: str,
    upload_id: str,
    file_ext: str,
    file_size: int,
    auth_key: str,
    task_id: str,
    cover_upload_id: Optional[str] = None,
    scheduled_at: Optional[str] = None,
) -> TencentPublishResult:
    """Step 8: Create the post via post_create.

    Builds the request body with video info, description, and metadata.
    """
    video_duration = _get_video_duration(video_path)

    # Build post content (simplified XML with description)
    post_content = _build_post_content(desc, tags)

    # Build video info
    video_width, video_height = _get_video_dimensions(video_path)

    md5sum = _file_md5(video_path)

    video_info = {
        "video": {
            "height": video_height,
            "width": video_width,
            "fileSize": file_size,
            "md5sum": md5sum,
            "duration": video_duration,
            "playLen": video_duration * 1000,
        }
    }

    # Build the publish request body (the oe object from yixiaoer)
    body = {
        "_log_finder_uin": uin,
        "_log_finder_id": finder_username,
        "rawKeyBuff": None,
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
        "postContent": post_content,
        "videoClipTaskId": task_id,
        "pubType": 1,  # 1 = video, 0 = image/dynamic
        **video_info,
    }

    # Cover if uploaded
    if cover_upload_id:
        body["coverUploadId"] = cover_upload_id

    # Scheduled publishing
    if scheduled_at:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(scheduled_at)
            effective_ts = int(dt.timestamp() * 1000)
            body["effectiveTime"] = effective_ts
        except (ValueError, ImportError):
            pass

    headers = {
        **_DEFAULT_HEADERS,
        "cookie": cookie_str,
        "Content-Type": "application/json",
    }

    logger.info(
        "[tencent_video] Creating post: title={}, desc_len={}",
        title, len(desc),
    )

    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    API["post_create"],
                    json=body,
                    headers=headers,
                )
                data = resp.json()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return TencentPublishResult(
                success=False, error=f"post_create request failed: {e}",
            )

        if isinstance(data, dict):
            err_code = data.get("errCode") or data.get("code", 0)
            err_msg = data.get("errMsg") or data.get("message", "")

            # Success
            if err_code == 0 or err_code == "0" or err_code is None:
                # Extract publish_id from response
                publish_id = _extract_publish_id(data)
                return TencentPublishResult(
                    success=True,
                    publish_id=publish_id,
                    url=f"https://channels.weixin.qq.com/platform/post/{publish_id}" if publish_id else None,
                )

            # Login expired
            if str(err_code) in ("300333", "300334") or "login" in str(err_msg).lower():
                return TencentPublishResult(
                    success=False, error=f"tencent_video login expired ({err_code})",
                )

            # Need real-name verification
            if err_code == -11224:
                return TencentPublishResult(
                    success=False, error="视频号需管理员实名认证后才可以发布",
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue

            return TencentPublishResult(
                success=False, error=f"post_create failed: [{err_code}] {err_msg}",
            )

        return TencentPublishResult(success=False, error="post_create: empty response")

    return TencentPublishResult(success=False, error="post_create: max retries exceeded")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _auth_data_body() -> dict:
    return {
        "timestamp": _timestamp(),
        "_log_finder_uin": "",
        "_log_finder_id": "",
        "rawKeyBuff": None,
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }


def _compute_chunk_sizes(file_size: int, num_chunks: int) -> list[int]:
    sizes = []
    for i in range(num_chunks):
        start = i * CHUNK_SIZE
        if i == num_chunks - 1:
            sizes.append(file_size - start)
        else:
            sizes.append(CHUNK_SIZE)
    return sizes


def _build_post_content(desc: str, tags: list[str]) -> str:
    """Build the post content XML string.

    Simplified version of yixiaoer's XMLWriter-based builder.
    For plain text descriptions without HTML/topic markup.
    """
    import xml.etree.ElementTree as ET

    finder = ET.Element("finder")
    version = ET.SubElement(finder, "version")
    version.text = "1"

    value_count = 0

    # Add description text
    text_content = desc.strip() or ""
    if text_content:
        elem = ET.SubElement(finder, f"value{value_count}")
        elem.text = f"<![CDATA[{text_content}]]>"
        value_count += 1
        elem = ET.SubElement(finder, f"value{value_count}")
        elem.text = "<![CDATA[ ]]>"
        value_count += 1

    # Add tags as topics
    for tag in tags:
        tag_text = f"#{tag.strip()}#"
        elem = ET.SubElement(finder, f"value{value_count}")
        elem.text = f"<![CDATA[{tag_text}]]>"
        value_count += 1
        elem = ET.SubElement(finder, f"value{value_count}")
        elem.text = "<![CDATA[ ]]>"
        value_count += 1

    # Finalize
    ET.SubElement(finder, "valuecount").text = str(value_count)

    xml_bytes = ET.tostring(finder, encoding="unicode", xml_declaration=False)
    return xml_bytes


def _extract_publish_id(data: dict) -> Optional[str]:
    """Extract publish ID from post_create response."""
    # The publish ID may be in various response fields
    inner = data.get("data", {})
    if inner.get("publishId"):
        return str(inner["publishId"])

    # From objectDesc or similar
    object_desc = inner.get("objectDesc", {})
    if object_desc.get("id"):
        return str(object_desc["id"])

    # Try finding encfilekey in response
    resp_text = json.dumps(data)
    m = re.search(r'encfilekey=([^&\s"\']+)', resp_text)
    if m:
        return m.group(1)

    # Fallback: use upload_id
    return None


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_video_duration(path: str) -> int:
    """Get video duration in seconds via ffprobe.

    Returns 10 as fallback if ffprobe unavailable.
    """
    try:
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(float(result.stdout.strip()))
    except Exception:
        pass
    return 10


def _get_video_dimensions(path: str) -> tuple[int, int]:
    """Get video dimensions via ffprobe.

    Returns (1080, 1920) as fallback (portrait 9:16).
    """
    try:
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            width, height = None, None
            for line in result.stdout.strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k == "width":
                        width = int(v)
                    elif k == "height":
                        height = int(v)
            if width and height:
                return width, height
    except Exception:
        pass
    return 1080, 1920
