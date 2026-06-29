"""Background pipeline: split → optimize → media → compose.

Called by POST /api/v1/aggregator/generate via FastAPI BackgroundTasks.
Runs in-process (Phase 1 SDK integration), no Celery dependency.
Compose step is gated by VideoConcurrencyController for OOM prevention.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiosqlite

from services.concurrency_control import video_concurrency

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────


def _scratch_dir(job_id: str) -> str:
    d = os.path.join("/tmp", "pipeline", job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _ratio_dims(ratio: str) -> tuple[int, int]:
    return {
        "9:16": (720, 1280),
        "16:9": (1280, 720),
        "1:1": (720, 720),
    }.get(ratio, (720, 1280))


# ─── DB helpers ──────────────────────────────────────────────────────


async def _update_status(db, job_id: str, status: str, **extra) -> None:
    if extra:
        sets = ", ".join(f"{k} = ?" for k in extra)
        await db.execute(
            f"UPDATE jobs SET status = ?, {sets} WHERE id = ?",
            (status, *extra.values(), job_id),
        )
    else:
        await db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    await db.commit()


async def _fail_job(db, job_id: str, error: str) -> None:
    await db.execute(
        "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
        (error[:500], job_id),
    )
    await db.commit()


# ─── Pipeline ────────────────────────────────────────────────────────


async def run_pipeline(
    db_path: str,
    job_id: str,
    content: str,
) -> None:
    """Execute split → optimize → TTS → image → compose.

    Phase 2: Delegates to ``pipeline_v2.run_block_pipeline()`` for Block Graph
    execution, replacing the previous hardcoded 5-step implementation.

    Opens its own DB connection (BackgroundTasks run after request closes).
    Reads voice/video_ratio/prompt_platform from ``jobs.input_data`` JSON.
    The Block engine handles per-step status updates ("splitting" → "optimizing"
    → "tts" → "imaging" → "composing" → "done") and writes them to ``jobs`` table
    so the frontend can poll progress.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT input_data FROM jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            logger.error("Job %s not found in DB", job_id)
            return
        cfg = json.loads(row["input_data"])
    finally:
        await db.close()

    voice = cfg.get("voice", "zh-CN-XiaoxiaoNeural")
    video_ratio = cfg.get("video_ratio", "9:16")
    prompt_platform = cfg.get("prompt_platform", "midjourney")

    logger.info(
        "Job %s: Block Graph pipeline (voice=%s, ratio=%s, platform=%s)",
        job_id, voice, video_ratio, prompt_platform,
    )

    from services.pipeline_v2 import run_block_pipeline

    result = await run_block_pipeline(
        db_path=db_path,
        job_id=job_id,
        content=content,
        voice=voice,
        prompt_platform=prompt_platform,
        video_ratio=video_ratio,
    )

    if result.success:
        logger.info("Job %s pipeline completed successfully", job_id)
    else:
        logger.error("Job %s pipeline failed: %s", job_id, result.node_errors)
