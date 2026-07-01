"""Aggregator API — N-in-1 unified frontend data endpoints.

Bundles multiple microservice calls into single round-trips
for the unified dashboard frontend. Backend-agnostic: all three
endpoints only query orchestrator-local state + pip-installed modules.
"""

from __future__ import annotations
import uuid

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from config import settings
from db import DB_PATH, get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature
from services.pipeline import run_pipeline

router = APIRouter(prefix="/api/v1/aggregator", tags=["aggregator"])


# ─── Request / Response models ───────────────────────────────────────


class GenerateRequest(BaseModel):
    model_config = {"extra": "forbid"}
    article_id: str = Field(..., description="Source article ID")
    voice: str = Field(default="zh-CN-XiaoxiaoNeural")
    video_ratio: str = Field(default="9:16")
    prompt_platform: str = Field(default="midjourney")
    publish_platforms: list[str] = Field(default_factory=list)


class BatchGenerateRequest(BaseModel):
    model_config = {"extra": "forbid"}
    article_ids: list[str] = Field(
        ..., min_length=1, max_length=20,
        description="Source article IDs (1-20 per batch)",
    )
    voice: str = Field(default="zh-CN-XiaoxiaoNeural")
    video_ratio: str = Field(default="9:16")
    prompt_platform: str = Field(default="midjourney")


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/dashboard")
async def get_dashboard(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Unified dashboard: trending preview + today's video stats."""
    trending: list[dict[str, Any]] = []
    today_stats = {"processing": 0, "done": 0, "failed": 0}

    # 1. Trending — best-effort, silently degrade
    # Skip in dev mode (SQLite) — PG not available locally
    if not settings.database_url.startswith("sqlite"):
        try:
            from trendscope.api.config import settings as ts_settings  # type: ignore
            from sqlalchemy import text as sa_text  # type: ignore
            from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore

            ts_engine = create_async_engine(
                ts_settings.database_url,
                connect_args={"connect_timeout": 2},
            )
            async with ts_engine.connect() as conn:
                rows = await conn.execute(
                    sa_text(
                        "SELECT title, platform_code, hot_score, url "
                        "FROM hot_articles WHERE is_active = true "
                        "ORDER BY hot_score DESC LIMIT 10"
                    )
                )
                trending = [dict(r) for r in rows.all()]
            await ts_engine.dispose()
        except Exception:
            trending = []

    # 2. Today's job stats
    try:
        async with db.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs "
            "WHERE date(created_at) = date('now') "
            "AND user_id = ? GROUP BY status",
            (current_user["sub"],),
        ) as cursor:
            for row in await cursor.fetchall():
                key = str(row["status"])
                if key in today_stats:
                    today_stats[key] = row["cnt"]
    except Exception:
        pass

    return {
        "trending": trending,
        "today_stats": today_stats,
        "user": {
            "id": current_user.get("sub"),
            "username": current_user.get("username"),
            "role": current_user.get("role", "user"),
        },
    }


@router.get("/generate-options")
async def get_generate_options(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Pre-populated options for video generation wizard."""
    options: dict[str, Any] = {
        "voices": [
            {"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓（女声）"},
            {"id": "zh-CN-YunxiNeural", "label": "云希（男声）"},
            {"id": "zh-CN-XiaoyiNeural", "label": "晓伊（女声）"},
            {"id": "zh-CN-YunjianNeural", "label": "云健（男声）"},
        ],
        "video_ratios": [
            {"id": "9:16", "label": "竖屏 9:16（抖音/快手）"},
            {"id": "16:9", "label": "横屏 16:9（B站/YouTube）"},
            {"id": "1:1", "label": "方形 1:1（小红书/公众号）"},
        ],
        "prompt_platforms": [
            {"id": "midjourney", "label": "Midjourney"},
            {"id": "stable_diffusion", "label": "Stable Diffusion"},
            {"id": "dall_e", "label": "DALL-E"},
            {"id": "sd_xl", "label": "SDXL"},
            {"id": "flux", "label": "Flux"},
            {"id": "kling", "label": "Kling"},
            {"id": "cogview", "label": "CogView"},
        ],
        "content_sources": [],
    }
    try:
        async with db.execute(
            "SELECT id, source_url, word_count_original, status "
            "FROM articles WHERE user_id = ? AND status IN ('draft','rewritten') "
            "ORDER BY created_at DESC LIMIT 20",
            (current_user["sub"],),
        ) as cursor:
            options["content_sources"] = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        pass
    return options


@router.post("/generate")
async def generate_pipeline(
    body: GenerateRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Submit one-step generate pipeline: split → prompt → video.

    Dispatches via BackgroundTasks; frontend polls /api/jobs/{id}
    for status progression: splitting → optimizing → tts →
    imaging → composing → done.
    """
    # 1. Fetch article
    async with db.execute(
        "SELECT * FROM articles WHERE id = ? AND user_id = ?",
        (body.article_id, current_user["sub"]),
    ) as cursor:
        article = await cursor.fetchone()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    content = article["result_content"] or article["source_content"]

    # 2. Create job record
    import json
    job_id = str(uuid.uuid4())
    input_data = json.dumps({
        "article_id": body.article_id,
        "voice": body.voice,
        "video_ratio": body.video_ratio,
        "prompt_platform": body.prompt_platform,
    })
    await db.execute(
        """INSERT INTO jobs (id, user_id, job_type, status, input_data, created_at)
           VALUES (?, ?, 'video', 'pending', ?, datetime('now'))""",
        (job_id, current_user["sub"], input_data),
    )
    await db.commit()

    # 3. Dispatch background pipeline (db-agnostic params)
    background_tasks.add_task(
        run_pipeline,
        db_path=DB_PATH,
        job_id=job_id,
        content=content,
    )

    return {"job_id": job_id, "status": "pending"}




@router.post("/batch-generate")
@requires_feature("batch_operations")
async def batch_generate_pipeline(
    body: BatchGenerateRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Batch generate: submit multiple articles in one request.
    
    Creates one job per valid article and dispatches BackgroundTasks.
    Returns list of {job_id, article_id, status} plus any missing article_ids.
    Protected by batch_operations feature gate.
    """
    # 1. Fetch all requested articles
    placeholders = ",".join("?" * len(body.article_ids))
    async with db.execute(
        f"SELECT * FROM articles WHERE id IN ({placeholders}) AND user_id = ?",
        (*body.article_ids, current_user["sub"]),
    ) as cursor:
        articles = await cursor.fetchall()
    
    found_ids = {a["id"] for a in articles}
    missing = [aid for aid in body.article_ids if aid not in found_ids]
    
    # 2. Create one job per article
    import json
    results = []
    for article in articles:
        content = article["result_content"] or article["source_content"]
        job_id = str(uuid.uuid4())
        input_data = json.dumps({
            "article_id": article["id"],
            "voice": body.voice,
            "video_ratio": body.video_ratio,
            "prompt_platform": body.prompt_platform,
        })
        await db.execute(
            """INSERT INTO jobs (id, user_id, job_type, status, input_data, created_at)
               VALUES (?, ?, 'video', 'pending', ?, datetime('now'))""",
            (job_id, current_user["sub"], input_data),
        )
        background_tasks.add_task(
            run_pipeline,
            db_path=DB_PATH,
            job_id=job_id,
            content=content,
        )
        results.append({"job_id": job_id, "article_id": article["id"], "status": "pending"})
    
    await db.commit()
    
    return {
        "results": results,
        "total": len(results),
        "missing": missing if missing else None,
    }


@router.post("/upload")
@requires_feature("file_upload")
async def upload_article_file(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Upload a text/markdown file as new article content.
    
    Supported formats: .txt, .md
    The file content is stored as a new article in 'draft' status,
    making it available for video generation in the Generate page.
    """
    # Validate file type
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in (".txt", ".md", ".markdown")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Please upload a .txt or .md file.",
        )
    
    # Read file content
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    
    article_id = str(uuid.uuid4())
    word_count = len(text)
    
    await db.execute(
        """INSERT INTO articles (id, user_id, source_type, source_url, source_content, word_count_original, status)
           VALUES (?, ?, 'upload', ?, ?, ?, 'draft')""",
        (article_id, current_user["sub"], file.filename or "upload", text, word_count),
    )
    await db.commit()
    
    return {
        "article_id": article_id,
        "filename": file.filename or "untitled",
        "word_count": word_count,
        "status": "draft",
    }
