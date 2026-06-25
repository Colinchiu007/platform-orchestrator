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

    Opens its own DB connection (BackgroundTasks run after request closes).
    Reads voice/video_ratio/prompt_platform from ``jobs.input_data`` JSON.
    Each step updates ``jobs.status`` so the frontend can poll progress.
    The compose step is submitted through VideoConcurrencyController.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    try:
        # Read config from job record
        async with db.execute(
            "SELECT input_data FROM jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            logger.error("Job %s not found in DB", job_id)
            return
        cfg = json.loads(row["input_data"])
        voice = cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        video_ratio = cfg.get("video_ratio", "9:16")
        prompt_platform = cfg.get("prompt_platform", "midjourney")

        # ── Step 1: Split ────────────────────────────────────────
        await _update_status(db, job_id, "splitting")
        scenes = await _run_splitter(content)
        if not scenes:
            await _fail_job(db, job_id, "分句器未返回任何场景")
            return

        # ── Step 2: Optimize ─────────────────────────────────────
        await _update_status(db, job_id, "optimizing")
        prompts = await _run_optimizer(scenes, prompt_platform)

        # ── Step 3: TTS ──────────────────────────────────────────
        await _update_status(db, job_id, "tts")
        scratch = _scratch_dir(job_id)
        audio_path = os.path.join(scratch, "audio.mp3")
        await _run_tts(scenes, voice, audio_path)

        # ── Step 4: Images ───────────────────────────────────────
        await _update_status(db, job_id, "imaging")
        image_paths = await _run_image_gen(prompts, scratch)

        # ── Step 5: Compose (via concurrency controller) ─────────
        await _update_status(db, job_id, "composing")

        width, height = _ratio_dims(video_ratio)
        output_path = os.path.join(scratch, f"{job_id}.mp4")

        async def _compose():
            from video_compositor import CompositorInput, compose_video

            inp = CompositorInput(
                images=image_paths,
                audio_path=audio_path,
                output_path=output_path,
                width=width,
                height=height,
            )
            result = await asyncio.to_thread(compose_video, inp)
            if not result.success:
                raise RuntimeError(f"合成失败: {result.error}")
            return result

        concurrency_result = await video_concurrency.submit(job_id, _compose)
        if concurrency_result == "rejected":
            await _fail_job(db, job_id, "系统繁忙，任务被拒绝（内存不足或队列满）")
            return

        # ── Done ─────────────────────────────────────────────────
        await _update_status(db, job_id, "done", output_path=output_path)

    except Exception as exc:
        logger.exception("Pipeline failed for job %s", job_id)
        await _fail_job(db, job_id, f"管道异常: {exc}")
    finally:
        await db.close()


# ─── Step implementations ────────────────────────────────────────────


async def _run_splitter(content: str) -> list[dict]:
    from splitter import SmartSentenceSplitter

    splitter = SmartSentenceSplitter({"language": "zh"})
    result = await asyncio.to_thread(splitter.split, content)
    return [
        {
            "text": s.text,
            "segment_id": s.segment_id,
            "estimated_duration": s.estimated_duration,
            "sentences": [st.text for st in s.sentences],
        }
        for s in result.scenes
    ]


async def _run_optimizer(scenes: list[dict], platform: str) -> list[str]:
    from prompt_engine import Optimizer, OptimizeRequest, PlatformType

    platform_map = {
        "midjourney": PlatformType.MIDJOURNEY,
        "stable_diffusion": PlatformType.STABLE_DIFFUSION,
        "dall_e": PlatformType.DALLE,
        "sd_xl": PlatformType.STABLE_DIFFUSION,
        "flux": PlatformType.GENERIC,
        "kling": PlatformType.GENERIC,
        "cogview": PlatformType.GENERIC,
    }
    pt = platform_map.get(platform, PlatformType.GENERIC)
    optimizer = Optimizer()

    prompts: list[str] = []
    for scene in scenes:
        req = OptimizeRequest(
            prompt=scene["text"][:1500],
            platform=pt,
            creative_level=5,
            max_length=300,
            num_candidates=1,
        )
        result = await asyncio.to_thread(optimizer.optimize, req)
        prompts.append(result.optimized_prompt or scene["text"])
    return prompts


async def _run_tts(scenes: list[dict], voice: str, output_path: str) -> None:
    from services.tts_service import text_to_speech

    full_text = " ".join(s["text"] for s in scenes)
    await text_to_speech(full_text, voice_name=voice, output_path=output_path)


async def _run_image_gen(prompts: list[str], scratch: str) -> list[str]:
    from services.image_service import generate_image

    paths: list[str] = []
    for i, prompt in enumerate(prompts):
        path = os.path.join(scratch, f"scene_{i:03d}.png")
        result = await generate_image(prompt, output_path=path)
        if result.status == "success":
            paths.append(result.image_url or path)
        else:
            logger.warning("Image gen failed for scene %d: %s", i, result.error)
    return paths
