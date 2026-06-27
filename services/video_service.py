"""Video generation service — unified interface for AI video providers.

Replaces 8 Edge Functions: kling-create-video, sora-create-video,
jimeng-create-video, vidu-create-video + query counterparts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from services.provider_router import get_router

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
    key = req.api_key
    if not key:
        router = get_router()
        cfg = await router.get("kling")
        if cfg:
            key = cfg["api_key"]
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
    key = req.api_key
    if not key:
        router = get_router()
        cfg = await router.get("jimeng")
        if cfg:
            key = cfg["api_key"]
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


# ── Retry Helper ─────────────────────────────────────────────────────────────


async def _retry_with_backoff(func, max_retries: int = 3):
    """Retry an async function with exponential backoff on HTTP/connect errors.

    Retries on httpx.HTTPStatusError and httpx.ConnectError.
    Non-HTTP errors propagate immediately.
    Backoff delays: 1s, 2s, 4s (2^(attempt-1)).
    max_retries is total attempts, including the first.
    """
    last_exception: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await func()
        except (httpx.HTTPStatusError, httpx.ConnectError) as exc:
            last_exception = exc
            if attempt < max_retries:
                await asyncio.sleep(2 ** (attempt - 1))
        except Exception:
            raise
    raise last_exception  # type: ignore[misc]


# ── Provider-specific Query Functions ────────────────────────────────────────


async def _query_kling_status(task_id: str, api_key: str) -> VideoResult:
    """Query Kling video generation status."""
    url = f"https://api.kling.kuaishou.com/v1/videos/text2video/{task_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    task_status = data.get("data", {}).get("task_status", "")

    if task_status == "submitted":
        return VideoResult(
            provider=VideoProvider.KLING, status=VideoStatus.PENDING,
            task_id=task_id, progress=10,
        )
    elif task_status == "processing":
        return VideoResult(
            provider=VideoProvider.KLING, status=VideoStatus.PROCESSING,
            task_id=task_id, progress=50,
        )
    elif task_status == "succeed":
        videos = data.get("data", {}).get("videos", [])
        video_url = videos[0].get("url", "") if videos else ""
        return VideoResult(
            provider=VideoProvider.KLING, status=VideoStatus.COMPLETED,
            task_id=task_id, video_url=video_url, progress=100,
        )
    elif task_status == "failed":
        return VideoResult(
            provider=VideoProvider.KLING, status=VideoStatus.FAILED,
            task_id=task_id,
        )
    else:
        return VideoResult(
            provider=VideoProvider.KLING, status=VideoStatus.FAILED,
            task_id=task_id, error=f"Unknown status: {task_status}",
        )


async def _query_jimeng_status(task_id: str, api_key: str) -> VideoResult:
    """Query Jimeng video generation status."""
    url = f"https://ark.cn-beijing.volces.com/api/v3/imaginations/generations/{task_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    status = data.get("status", "")

    if status == "pending":
        return VideoResult(
            provider=VideoProvider.JIMENG, status=VideoStatus.PENDING,
            task_id=task_id,
        )
    elif status == "processing":
        return VideoResult(
            provider=VideoProvider.JIMENG, status=VideoStatus.PROCESSING,
            task_id=task_id,
        )
    elif status == "completed":
        results = data.get("results", [])
        video_url = results[0].get("url", "") if results else ""
        return VideoResult(
            provider=VideoProvider.JIMENG, status=VideoStatus.COMPLETED,
            task_id=task_id, video_url=video_url, progress=100,
        )
    elif status == "failed":
        return VideoResult(
            provider=VideoProvider.JIMENG, status=VideoStatus.FAILED,
            task_id=task_id,
        )
    else:
        return VideoResult(
            provider=VideoProvider.JIMENG, status=VideoStatus.FAILED,
            task_id=task_id, error=f"Unknown status: {status}",
        )


# ── Query Functions ─────────────────────────────────────────────────────────


async def query_video_status(
    task_id: str,
    provider: VideoProvider,
    api_key: Optional[str] = None,
) -> VideoResult:
    """Query the status of an async video generation task."""
    # Resolve API key from parameter or ProviderRouter
    resolved_key: str | None = api_key
    if resolved_key is None:
        router = get_router()
        provider_name_map = {
            VideoProvider.KLING: "kling",
            VideoProvider.JIMENG: "jimeng",
        }
        pname = provider_name_map.get(provider)
        if pname:
            cfg = await router.get(pname)
            if cfg:
                resolved_key = cfg["api_key"]

    if not resolved_key:
        return VideoResult(
            provider=provider, status=VideoStatus.FAILED,
            task_id=task_id, error="No API key provided",
        )

    # Dispatch to provider-specific query with retry
    try:
        if provider == VideoProvider.KLING:
            return await _retry_with_backoff(
                lambda: _query_kling_status(task_id, resolved_key),  # type: ignore[arg-type]
                max_retries=3,
            )
        elif provider == VideoProvider.JIMENG:
            return await _retry_with_backoff(
                lambda: _query_jimeng_status(task_id, resolved_key),  # type: ignore[arg-type]
                max_retries=3,
            )
        else:
            return VideoResult(
                provider=provider, status=VideoStatus.FAILED,
                task_id=task_id, error=f"Unsupported provider: {provider}",
            )
    except Exception as exc:
        return VideoResult(
            provider=provider, status=VideoStatus.FAILED,
            task_id=task_id, error=str(exc),
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
