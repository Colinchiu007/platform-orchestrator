"""Video generation job router — orchestrates the full Story2Video pipeline.

Pipeline: article → split → TTS → prompt optimize → image gen → compositing → video

Endpoints:
- POST /api/jobs/video — create full video generation job
- GET  /api/jobs/video/{id} — get job status/progress
- GET  /api/jobs/video/ — list video jobs
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature
from middleware.rate_limit import limiter, rate_limit_video

router = APIRouter()


class CreateVideoRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to generate video from")
    image_effect: str = Field(default="zoom-in", description="Image effect")
    transition: str = Field(default="fade", description="Transition effect")
    voice_id: str = Field(default="zh_female_qingxinnvsheng_uranus_bigtts")
    image_provider: str = Field(default="minimax", description="Image gen provider")


class CreateStory2VideoRequest(BaseModel):
    article_id: str = Field(..., description="Article ID to generate story2video from")


@router.post("/video")
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

    job_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data)
           VALUES (?, ?, 'video', 'queued', ?)""",
        (job_id, current_user["sub"], json.dumps({
            "article_id": body.article_id,
            "image_effect": body.image_effect,
            "transition": body.transition,
        })),
    )
    await db.commit()

    # Launch async pipeline
    background_tasks.add_task(
        _run_video_pipeline,
        job_id=job_id,
        article_id=body.article_id,
        split_json=json.loads(split_row["result_json"]),
        image_effect=body.image_effect,
        transition=body.transition,
        voice_id=body.voice_id,
        image_provider=body.image_provider,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": (
            "Video generation started. "
            "Poll GET /api/jobs/video/{job_id} for progress."
        ),
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

    job_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data)
           VALUES (?, ?, 'story2video', 'queued', ?)""",
        (job_id, current_user["sub"], json.dumps({
            "article_id": body.article_id,
        })),
    )
    await db.commit()

    # Launch async pipeline
    output_dir = f"output/story2video/{job_id}"
    os.makedirs(output_dir, exist_ok=True)

    background_tasks.add_task(
        _run_story2video_background,
        job_id=job_id,
        article_text=article_text,
        output_dir=output_dir,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": (
            "Story2Video job created. "
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
    """Background task: run the full TTS → image gen → compositing pipeline."""
    import aiosqlite
    from services.tts_service import text_to_speech
    from services.prompt_service import optimize_prompts_batch
    from services.image_service import generate_images_batch, ImageProvider
    from services.compositor import compose_video, CompositorInput, SubtitleSegment

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
        scenes = split_json.get("scenes", [])
        if not scenes:
            await _update("failed", error="No scenes in split result")
            return

        # ── 1. TTS: Generate audio from full article text ───────────────
        await _update("generating_audio", {"progress": 0.1})

        full_text = " ".join(s.get("text", "") for s in scenes)
        tts_result = await text_to_speech(text=full_text, voice_id=voice_id)

        if tts_result.error:
            await _update("failed", error=f"TTS failed: {tts_result.error}")
            return

        # ── 2. Prompt optimization ──────────────────────────────────────
        await _update("generating_images", {"progress": 0.3})

        scene_texts = [s.get("text", "") for s in scenes]
        prompt_result = await optimize_prompts_batch(
            [{"text": t} for t in scene_texts]
        )

        optimized_prompts = (
            prompt_result.prompts if not prompt_result.error else scene_texts
        )
        if len(optimized_prompts) < len(scene_texts):
            optimized_prompts += scene_texts[len(optimized_prompts):]

        # ── 3. Image generation ─────────────────────────────────────────
        valid_providers = [p.value for p in ImageProvider]
        provider = (
            ImageProvider(image_provider)
            if image_provider in valid_providers
            else ImageProvider.MINIMAX
        )

        image_results = await generate_images_batch(
            prompts=optimized_prompts,
            provider=provider,
        )

        image_paths = [r.local_path for r in image_results if r.local_path]
        if not image_paths:
            await _update("failed", error="All image generations failed")
            return

        await _update("compositing", {
            "progress": 0.7, "images_generated": len(image_paths),
        })

        # ── 4. Build subtitle segments from split result ────────────────
        subtitle_segments = []
        for scene in scenes:
            for sub in scene.get("subtitles", []):
                subtitle_segments.append(SubtitleSegment(
                    text=sub.get("text", ""),
                    start_time=sub.get("start_time", 0),
                    end_time=sub.get("start_time", 0) + sub.get("duration", 2),
                ))

        # ── 5. Compositing ──────────────────────────────────────────────
        output_path = f"output/videos/{job_id}.mp4"

        composit_result = compose_video(CompositorInput(
            images=image_paths,
            audio_path=tts_result.audio_path,
            output_path=output_path,
            image_effect=image_effect,
            transition=transition,
            subtitles=subtitle_segments,
        ))

        if not composit_result.success:
            await _update(
                "failed", error=f"Compositing failed: {composit_result.error}"
            )
            return

        await _update("done", {
            "progress": 1.0,
            "output_path": output_path,
            "duration": composit_result.duration_seconds,
            "scenes": len(scenes),
            "images_generated": len(image_paths),
        })

    except Exception as e:
        await _update("failed", error=str(e))


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
