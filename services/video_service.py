"""Video generation service — unified interface for AI video providers.

Replaces 8 Edge Functions: kling-create-video, sora-create-video,
jimeng-create-video, vidu-create-video + query counterparts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from config import settings

OUTPUT_DIR = Path("output/videos")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Enums ───────────────────────────────────────────────────────────────────


class VideoProvider(str, Enum):
    KLING = "kling"
    SORA = "sora"
    JIMENG = "jimeng"
    VIDU = "vidu"


class VideoStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class VideoResult:
    provider: VideoProvider
    status: VideoStatus
    video_url: str = ""
    local_path: str = ""
    task_id: str = ""
    progress: int = 0
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class GenerateVideoRequest:
    prompt: str
    provider: VideoProvider = VideoProvider.KLING
    mode: str = "text-to-video"   # text-to-video | image-to-video
    size: str = "720x1280"
    seconds: int = 5
    model: str = ""
    input_reference_url: Optional[str] = None
    negative_prompt: Optional[str] = None
    api_key: Optional[str] = None


# ── Provider Implementations ────────────────────────────────────────────────


async def _generate_kling(req: GenerateVideoRequest) -> VideoResult:
    """Kling video generation — async, returns task_id."""
    key = req.api_key or settings.kling_api_key
    if not key:
        return VideoResult(provider=VideoProvider.KLING, status=VideoStatus.FAILED, error="No API key")

    payload = {
        "model_name": req.model or "kling-v1",
        "prompt": req.prompt,
        "duration": str(req.seconds),
        "mode": "std",
    }
    if req.mode == "image-to-video" and req.input_reference_url:
        payload["image"] = req.input_reference_url
    if req.negative_prompt:
        payload["negative_prompt"] = req.negative_prompt

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.kling.kuaishou.com/v1/videos/text2video",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    task_id = data.get("data", {}).get("task_id", "")
    return VideoResult(
        provider=VideoProvider.KLING, status=VideoStatus.PENDING,
        task_id=task_id,
    )


async def _generate_jimeng(req: GenerateVideoRequest) -> VideoResult:
    """Jimeng video generation — returns task_id or direct URL."""
    key = req.api_key or settings.jimeng_api_key
    if not key:
        return VideoResult(provider=VideoProvider.JIMENG, status=VideoStatus.FAILED, error="No API key")

    payload = {
        "model": req.model or "jimeng-video-generate-3.0",
        "prompt": req.prompt,
        "size": req.size,
        "duration": req.seconds,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://ark.cn-beijing.volces.com/api/v3/imaginations/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    task_id = data.get("id", "")
    return VideoResult(
        provider=VideoProvider.JIMENG, status=VideoStatus.PENDING,
        task_id=task_id,
    )


# ── Query Functions ─────────────────────────────────────────────────────────


async def query_video_status(
    task_id: str,
    provider: VideoProvider,
    api_key: Optional[str] = None,
) -> VideoResult:
    """Query the status of an async video generation task."""
    # Placeholder — actual implementation depends on provider-specific query APIs
    return VideoResult(
        provider=provider, status=VideoStatus.PROCESSING,
        task_id=task_id, progress=50,
    )


# ── Public API ──────────────────────────────────────────────────────────────


_PROVIDERS = {
    VideoProvider.KLING: _generate_kling,
    VideoProvider.JIMENG: _generate_jimeng,
}


async def generate_video(req: GenerateVideoRequest) -> VideoResult:
    """Generate a video using the specified provider.

    Returns a VideoResult with task_id for async polling,
    or completed result for sync providers.
    """
    handler = _PROVIDERS.get(req.provider)
    if not handler:
        return VideoResult(provider=req.provider, status=VideoStatus.FAILED, error=f"Unsupported: {req.provider}")

    return await handler(req)
