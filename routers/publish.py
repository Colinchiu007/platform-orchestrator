"""Multi-platform publish router.

Endpoints:
- POST  /api/jobs/publish — create publish task (WeChat MP)
- POST  /api/jobs/publish-video — create video publish task (B站, 抖音, etc.)
- GET   /api/jobs/publish/{id} — get publish status
- GET   /api/jobs/publish/ — list publish tasks
- POST  /api/jobs/cookies/{platform} — receive cookies from desktop client
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user, get_current_user_or_api_key
from middleware.feature_gate import requires_feature
from services.provider_router import get_router

router = APIRouter()


class PublishRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to publish")
    platforms: list[str] = Field(default=["wechat_mp"], description="Target platforms")
    cover_image_path: Optional[str] = Field(
        default=None, description="Cover image path"
    )


class VideoPublishRequest(BaseModel):
    video_url: str = Field(..., description="Video file URL (Supabase storage)")
    title: str = Field(..., description="Video title (1-80 chars)")
    platform: str = Field(default="bilibili", description="Target platform: bilibili")
    desc: str = Field(default="", description="Video description")
    tags: list[str] = Field(default=[], description="Up to 12 tags")
    cover_url: Optional[str] = Field(default=None, description="Cover image URL")


@router.post("/publish")
@requires_feature("publish_single_platform")
async def create_publish_task(
    body: PublishRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user_or_api_key),
    db=Depends(get_db),
):
    """Create a publish task for the article."""
    sql = (
        "SELECT id, result_content, source_content, source_url "
        "FROM articles WHERE id = ? AND user_id = ?"
    )
    async with db.execute(sql, (body.article_id, current_user["sub"]),) as cursor:
        article = await cursor.fetchone()

    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    content = article["result_content"] or article["source_content"] or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="Article has no content")

    task_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data)
           VALUES (?, ?, 'publish', 'pending', ?)""",
        (task_id, current_user["sub"], json.dumps({
            "article_id": body.article_id,
            "platforms": body.platforms,
        })),
    )
    await db.commit()

    if "wechat_mp" in body.platforms:
        background_tasks.add_task(
            _publish_wechat,
            task_id=task_id,
            title=article["source_url"] or "Article",
            content=content,
            source_url=article["source_url"] or "",
            cover_path=body.cover_image_path,
        )

    return {
        "task_id": task_id,
        "status": "pending",
        "platforms": body.platforms,
        "message": "Publish task created",
    }


@router.get("/publish/pending")
async def get_pending_publish_tasks(
    db=Depends(get_db),
):
    """Get pending video_publish tasks (FIFO order).

    Internal API for Multi-Publish PublishPoller polling.
    No auth required — used by desktop app to poll for pending cloud publish tasks.
    """
    async with db.execute(
        """SELECT id, user_id, job_type, status, input_data, created_at, updated_at
           FROM jobs WHERE job_type = 'video_publish' AND status = 'pending'
           ORDER BY created_at ASC"""
    ) as cursor:
        rows = await cursor.fetchall()

    items = []
    for r in rows:
        item = dict(r)
        try:
            item["input_data"] = json.loads(item.get("input_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["input_data"] = {}
        items.append(item)

    return {"items": items}




@router.get("/publish/{task_id}")
async def get_publish_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_api_key),
    db=Depends(get_db),
):
    """Get publish task status."""
    async with db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (task_id, current_user["sub"]),
    ) as cursor:
        job = await cursor.fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": job["id"],
        "status": job["status"],
        "output_data": json.loads(job["output_data"]) if job["output_data"] else {},
        "error": job["error"],
        "created_at": job["created_at"],
    }


@router.get("/publish")
async def list_publish_tasks(
    current_user: dict = Depends(get_current_user_or_api_key),
    db=Depends(get_db),
):
    """List user's publish tasks."""
    async with db.execute(
        """SELECT id, job_type, status, created_at, updated_at, input_data
           FROM jobs WHERE user_id = ? AND job_type IN ('publish', 'video_publish')
           ORDER BY created_at DESC LIMIT 20""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()

    items = []
    for r in rows:
        item = dict(r)
        try:
            item["input_data"] = json.loads(item.get("input_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["input_data"] = {}
        items.append(item)

    return {"items": items}


@router.post("/publish-video")
@requires_feature("video_publish")
async def create_video_publish_task(
    body: VideoPublishRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user_or_api_key),
    db=Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Create a video publish task.

    Accepts a video URL (from Supabase storage) and publishes to
    the specified platform (B站, etc.).

    Auth: JWT Bearer token or X-API-Key header.
    """
    supported = ("bilibili", "douyin")
    if body.platform not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform '{body.platform}'. Supported: {', '.join(supported)}",
        )

    task_id = str(uuid.uuid4())

    await db.execute(
        "INSERT INTO jobs (id, user_id, job_type, status, input_data) "
        "VALUES (?, ?, 'video_publish', 'pending', ?)",
        (task_id, current_user["sub"], json.dumps({
            "video_url": body.video_url,
            "title": body.title,
            "platform": body.platform,
            "desc": body.desc,
            "tags": body.tags,
            "cover_url": body.cover_url,
        })),
    )
    await db.commit()

    background_tasks.add_task(
        _publish_video,
        task_id=task_id,
        video_url=body.video_url,
        title=body.title,
        platform=body.platform,
        desc=body.desc,
        tags=body.tags,
        cover_url=body.cover_url,
    )

    return {
        "task_id": task_id,
        "status": "pending",
        "platform": body.platform,
        "title": body.title,
        "message": "Video publish task created",
    }


@router.post("/cookies/{platform}")
async def push_platform_cookies(
    platform: str,
    body: dict,
    current_user: dict = Depends(get_current_user_or_api_key),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Push login cookies from desktop client to orchestrator storage.

    Called by Multi-Publish desktop app after B站/抖音 login completes.
    Cookies are stored in the provider_configs table under config.cookies
    and picked up by publish services (douyin_publisher, bilibili_publisher).

    Auth: JWT Bearer token or X-API-Key header (ORCHESTRATOR_API_KEY in desktop env).
    """
    supported = ("douyin", "bilibili")
    if platform not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform '{platform}'. Supported: {', '.join(supported)}",
        )

    cookies = body.get("cookies", [])
    if not cookies:
        raise HTTPException(status_code=400, detail="No cookies provided")

    router = get_router()
    cfg = await router.get(platform)

    provider_data = {
        "config": {"cookies": cookies},
    }

    if cfg:
        await router.update(platform, provider_data)
    else:
        display_names = {"douyin": "抖音", "bilibili": "B站"}
        base_urls = {
            "douyin": "https://creator.douyin.com",
            "bilibili": "https://member.bilibili.com",
        }
        await router.create({
            "name": platform,
            "provider_type": "platform",
            "display_name": display_names.get(platform, platform),
            "base_url": base_urls.get(platform, f"https://{platform}.com"),
            "api_key": platform,
            "config": {"cookies": cookies},
            "enabled": True,
            "min_tier": 1,
        })

    username = body.get("username", "")
    return {
        "status": "ok",
        "platform": platform,
        "username": username,
        "cookie_count": len(cookies),
    }


async def _publish_video(
    task_id: str,
    video_url: str,
    title: str,
    platform: str,
    desc: str = "",
    tags: Optional[list[str]] = None,
    cover_url: Optional[str] = None,
):
    """Background task: download video from URL and publish to platform."""
    import aiosqlite

    tags = tags or []

    async def _update(status: str, output: dict = None, error: str = None):
        db = await aiosqlite.connect("orchestrator.db")
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            "UPDATE jobs SET status = ?, output_data = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, json.dumps(output or {}), error, task_id),
        )
        await db.commit()
        await db.close()

    tmp_dir = tempfile.mkdtemp(prefix="video_publish_")

    try:
        await _update("downloading", {
            "phase": "download", "percent": 10, "message": "Downloading video...",
        })

        video_ext = Path(video_url.split("?")[0]).suffix or ".mp4"
        local_path = os.path.join(tmp_dir, f"video{video_ext}")

        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)

        file_size_mb = os.path.getsize(local_path) / 1024 / 1024

        cover_local = None
        if cover_url:
            try:
                resp = await client.get(cover_url, timeout=60.0)
                if resp.status_code == 200:
                    cover_ext = Path(cover_url.split("?")[0]).suffix or ".jpg"
                    cover_local = os.path.join(tmp_dir, f"cover{cover_ext}")
                    with open(cover_local, "wb") as f:
                        f.write(resp.content)
            except Exception:
                pass

        await _update("publishing", {
            "phase": "publish",
            "percent": 30,
            "message": f"Publishing to {platform} ({file_size_mb:.0f}MB)...",
        })

        if platform == "bilibili":
            from services.bilibili_publisher import publish_video

            result = await publish_video(
                title=title,
                video_path=local_path,
                cover_path=cover_local,
                desc=desc,
                tags=tags,
            )
        elif platform == "douyin":
            from services.douyin_publisher import publish_video

            result = await publish_video(
                title=title,
                video_path=local_path,
                cover_path=cover_local,
                desc=desc,
                tags=tags,
            )
        else:
            result = type("_Result", (), {
                "success": False,
                "publish_id": None,
                "url": None,
                "error": f"Unsupported platform: {platform}",
                "duration": 0.0,
            })()

        if result.success:
            await _update("success", {
                "platform": platform,
                "publish_id": result.publish_id,
                "url": result.url,
                "duration": result.duration,
                "file_size_mb": round(file_size_mb, 1),
            })
        else:
            await _update("failed", error=result.error)

    except httpx.HTTPStatusError as e:
        await _update("failed", error=f"Download failed: HTTP {e.response.status_code}")
    except httpx.TimeoutException:
        await _update("failed", error="Download timeout (video file too large?)")
    except Exception as e:
        await _update("failed", error=str(e))
    finally:
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


async def _publish_wechat(
    task_id: str,
    title: str,
    content: str,
    source_url: str = "",
    cover_path: Optional[str] = None,
):
    """Background task: publish to WeChat MP."""
    import aiosqlite
    from services.publish_service import publish_to_wechat

    async def _update(status: str, output: dict = None, error: str = None):
        db = await aiosqlite.connect("orchestrator.db")
        await db.execute("PRAGMA journal_mode=WAL;")
        sql = (
            "UPDATE jobs SET status = ?, output_data = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ?"
        )
        await db.execute(sql, (status, json.dumps(output or {}), error, task_id),)
        await db.commit()
        await db.close()

    try:
        html_content = _markdown_to_html(content)

        result = await publish_to_wechat(
            title=title,
            content_html=html_content,
            cover_image_path=cover_path,
            source_url=source_url,
        )

        if result.success:
            await _update("success", {
                "platform": "wechat_mp",
                "publish_id": result.publish_id,
                "article_url": result.article_url,
            })
        else:
            await _update("failed", error=result.error)

    except Exception as e:
        await _update("failed", error=str(e))


def _markdown_to_html(md: str) -> str:
    """Simple markdown-to-HTML for WeChat (just paragraphs + bold)."""
    import re

    lines = md.strip().split("\n")
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        html_parts.append(f"<p>{line}</p>")
    return "\n".join(html_parts)
