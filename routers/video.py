"""Video generation job router — orchestrates the full Story2Video pipeline.

Pipeline: article → split → TTS → prompt optimize → image gen → compositing → video

Endpoints:
- POST /api/jobs/video — create full video generation job
- GET  /api/jobs/video/{id} — get job status/progress
- GET  /api/jobs/video/ — list video jobs

Concurrency: video tasks are strictly serialized (max 1 concurrent) via
VideoConcurrencyController. Controlled by feature gate video_concurrency_control.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from functools import partial

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import load_feature_gates, requires_feature
from middleware.rate_limit import limiter, rate_limit_video
from services.concurrency_control import video_concurrency
from services.quota import increment_usage, QuotaExceededError

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Concurrency Gate Helper ──────────────────────────────────────────────────


def _sync_concurrency_gate() -> None:
    """Sync the video_concurrency_control feature gate to the controller."""
    gates = load_feature_gates()
    cc = gates.get("video_concurrency_control", {})
    video_concurrency.enabled = cc.get("enabled", True)


async def _submit_with_concurrency_control(
    job_id: str,
    coro_factory,
    db,
) -> str:
    """Submit a video task to the concurrency controller and update DB status.

    Returns "processing", "queued", or raises HTTPException(429) if rejected.
    """
    _sync_concurrency_gate()

    status = await video_concurrency.submit(job_id, coro_factory)

    if status == "rejected":
        # Rollback: remove the DB record
        await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
        queue_size = video_concurrency.queue_size
        if video_concurrency.active_count >= video_concurrency.MAX_CONCURRENT:
            detail = (
                f"Too many video jobs in queue ({queue_size}). "
                "Please wait for the current job to complete and try again."
            )
        else:
            detail = "Insufficient memory. Please try again later."
        raise HTTPException(status_code=429, detail=detail)

    elif status == "queued":
        await db.execute(
            "UPDATE jobs SET status = 'queued', updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        await db.commit()
    else:
        # "processing" — background coroutine handles status updates
        await db.execute(
            "UPDATE jobs SET status = 'processing', updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        await db.commit()

    return status


class CreateVideoRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to generate video from")
    image_effect: str = Field(default="zoom-in", description="Image effect")
    transition: str = Field(default="fade", description="Transition effect")
    voice_id: str = Field(default="zh_female_qingxinnvsheng_uranus_bigtts")
    image_provider: str = Field(default="minimax", description="Image gen provider")


class CreateStory2VideoRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to generate story2video from")


@router.post("/video")
@requires_feature("video_full_pipeline")
@requires_feature("video_fixed_template")
@limiter.limit(rate_limit_video)
async def create_video_job(
    request: Request,
    body: CreateVideoRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a video generation job.

    The job runs asynchronously via BackgroundTasks.
    Poll GET /api/jobs/video/{job_id} for progress.
    """
    # Verify article exists and has split result
    sql = (
        "SELECT id, result_content, source_content "
        "FROM articles WHERE id = ? AND user_id = ?"
    )
    async with db.execute(sql, (body.article_id, current_user["sub"]),) as cursor:
        article = await cursor.fetchone()

    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    async with db.execute(
        "SELECT result_json FROM splits WHERE article_id = ?",
        (body.article_id,),
    ) as cursor:
        split_row = await cursor.fetchone()

    if not split_row:
        raise HTTPException(
            status_code=400,
            detail=(
                "Article has not been split yet. "
                "POST /api/articles/{id}/split first."
            )
        )

    # Check daily quota before creating video
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        await increment_usage(db, current_user["sub"], today)
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=429,
            detail=e.message,
        )

    job_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data)
           VALUES (?, ?, 'video', 'pending', ?)""",
        (job_id, current_user["sub"], json.dumps({
            "article_id": body.article_id,
            "image_effect": body.image_effect,
            "transition": body.transition,
        })),
    )
    await db.commit()

    # Build coroutine factory for concurrency-controlled execution
    async def _run():
        await _run_video_pipeline(
            job_id=job_id,
            article_id=body.article_id,
            split_json=json.loads(split_row["result_json"]),
            image_effect=body.image_effect,
            transition=body.transition,
            voice_id=body.voice_id,
            image_provider=body.image_provider,
        )

    status = await _submit_with_concurrency_control(job_id, _run, db)

    return {
        "job_id": job_id,
        "status": status,
        "message": (
            "Video generation started. "
            "Poll GET /api/jobs/video/{job_id} for progress."
        ) if status == "processing" else (
            "Video job queued. "
            "Poll GET /api/jobs/video/{job_id} for progress."
        ),
    }


@router.get("/queue-status")
async def get_queue_status(
    current_user: dict = Depends(get_current_user),
):
    """Get current video task queue status.

    Returns active count, queue depth, and memory status.
    Does not require authentication for monitoring purposes.
    """
    import psutil
    mem = psutil.virtual_memory()
    return {
        "active_tasks": video_concurrency.active_count,
        "max_concurrent": video_concurrency.MAX_CONCURRENT,
        "queue_size": video_concurrency.queue_size,
        "max_queue_size": video_concurrency.MAX_QUEUE_SIZE,
        "concurrency_enabled": video_concurrency.enabled,
        "memory": {
            "available_mb": round(mem.available / (1024 * 1024)),
            "threshold_mb": video_concurrency.MEMORY_THRESHOLD_MB,
        },
    }


@router.get("/video/{job_id}")
async def get_video_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get video job status and progress."""
    async with db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, current_user["sub"]),
    ) as cursor:
        job = await cursor.fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job["id"],
        "status": job["status"],
        "input_data": json.loads(job["input_data"]),
        "output_data": json.loads(job["output_data"]) if job["output_data"] else {},
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@router.get("/video")
async def list_video_jobs(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """List user's video jobs."""
    offset = (page - 1) * page_size
    async with db.execute(
        """SELECT id, status, created_at, updated_at
           FROM jobs WHERE user_id = ? AND job_type = 'video'
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (current_user["sub"], page_size, offset),
    ) as cursor:
        rows = await cursor.fetchall()

    return {"items": [dict(r) for r in rows], "page": page, "page_size": page_size}


# ── Story2Video Pipeline Endpoint ──────────────────────────────────────────


@router.post("/story2video")
@requires_feature("video_full_pipeline")
@requires_feature("video_fixed_template")
async def create_story2video_job(
    request: Request,
    body: CreateStory2VideoRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a story2video generation job.

    Uses the fixed-template pipeline (text → segmentation → TTS → audio mix
    → image gen → slideshow → composite). Runs via BackgroundTasks.
    """
    # Verify article exists and belongs to user
    sql = (
        "SELECT id, result_content, source_content "
        "FROM articles WHERE id = ? AND user_id = ?"
    )
    async with db.execute(sql, (body.article_id, current_user["sub"]),) as cursor:
        article = await cursor.fetchone()

    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # Use result_content (LLM rewrite) if available, otherwise source
    article_text = article["result_content"] or article["source_content"] or ""
    if not article_text.strip():
        raise HTTPException(status_code=400, detail="Article has no content")


    # Check daily quota before creating video
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        await increment_usage(db, current_user["sub"], today)
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=429,
            detail=e.message,
        )
    job_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data)
           VALUES (?, ?, 'story2video', 'pending', ?)""",
        (job_id, current_user["sub"], json.dumps({
            "article_id": body.article_id,
        })),
    )
    await db.commit()

    output_dir = f"output/story2video/{job_id}"
    os.makedirs(output_dir, exist_ok=True)

    # Build coroutine factory for concurrency-controlled execution
    async def _run():
        await _run_story2video_background(
            job_id=job_id,
            article_text=article_text,
            output_dir=output_dir,
        )

    status = await _submit_with_concurrency_control(job_id, _run, db)

    return {
        "job_id": job_id,
        "status": status,
        "message": (
            "Story2Video job started. "
            "Poll GET /api/jobs/video/{job_id} for progress."
        ) if status == "processing" else (
            "Story2Video job queued. "
            "Poll GET /api/jobs/video/{job_id} for progress."
        ),
    }


# ── Background Pipeline ─────────────────────────────────────────────────────


async def _run_video_pipeline(
    job_id: str,
    article_id: str,
    split_json: dict,
    image_effect: str,
    transition: str,
    voice_id: str,
    image_provider: str,
):
    """Background task: run the full pipeline via Block engine (pipeline_v2).

    pipeline_v2 feature gate is permanently enabled. The original inline
    v1 implementation was removed in feat/optimize-6-items.
    """
    from services.pipeline_v2 import run_pipeline_v2

    try:
        await run_pipeline_v2(
            job_id=job_id,
            article_id=article_id,
            split_json=split_json,
            image_effect=image_effect,
            transition=transition,
            voice_id=voice_id,
            image_provider=image_provider,
        )
    except Exception as e:
        logger.error("Pipeline v2 failed: job=%s error=%s", job_id, e)


# ── Story2Video Background Task ─────────────────────────────────────────────


async def _run_story2video_background(
    job_id: str,
    article_text: str,
    output_dir: str,
):
    """Background task: run the fixed-template Story2Video pipeline."""
    import aiosqlite
    from services.story2video.pipeline import run_story2video_pipeline

    async def _update(status: str, output: dict = None, error: str = None):
        db = await aiosqlite.connect("orchestrator.db")
        await db.execute("PRAGMA journal_mode=WAL;")
        sql = (
            "UPDATE jobs SET status = ?, output_data = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ?"
        )
        await db.execute(sql, (status, json.dumps(output or {}), error, job_id),)
        await db.commit()
        await db.close()

    try:
        await _update("processing", {"progress": 0.0})

        result = await run_story2video_pipeline(
            article_text=article_text,
            output_dir=output_dir,
        )

        if not result.success:
            await _update("failed", error=result.error or "Pipeline failed")
            return

        await _update("done", {
            "progress": 1.0,
            "output_path": result.output_path,
            "scenes": result.scenes,
            "images_generated": result.images_generated,
            "duration_seconds": result.duration_seconds,
        })

    except Exception as e:
        await _update("failed", error=str(e))
