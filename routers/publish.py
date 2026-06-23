"""Multi-platform publish router.

Endpoints:
- POST /api/jobs/publish — create publish task (WeChat MP)
- GET  /api/jobs/publish/{id} — get publish status
- GET  /api/jobs/publish/ — list publish tasks
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature

router = APIRouter()


class PublishRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to publish")
    platforms: list[str] = Field(default=["wechat_mp"], description="Target platforms")
    cover_image_path: Optional[str] = Field(
        default=None, description="Cover image path"
    )


@router.post("/publish")
@requires_feature("publish_single_platform")
async def create_publish_task(
    body: PublishRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a publish task for the article."""
    # Get article content
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

    # Launch publish in background
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


@router.get("/publish/{task_id}")
async def get_publish_status(
    task_id: str,
    current_user: dict = Depends(get_current_user),
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
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """List user's publish tasks."""
    async with db.execute(
        """SELECT id, status, created_at, updated_at
           FROM jobs WHERE user_id = ? AND job_type = 'publish'
           ORDER BY created_at DESC LIMIT 20""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()

    return {"items": [dict(r) for r in rows]}


# ── Background Task ─────────────────────────────────────────────────────────


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
        # Convert markdown content to simple HTML for WeChat
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
        # Bold: **text** → <strong>text</strong>
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        html_parts.append(f"<p>{line}</p>")
    return "\n".join(html_parts)
