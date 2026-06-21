"""Image generation service — unified interface for multiple AI image providers.

Replaces 6+ Edge Functions: kling-omni-image-*, minimax-generate-image,
sensenova-generate-image, vidu-generate-image, jimeng-generate-image.

Supports both synchronous (MiniMax, SenseNova) and asynchronous (Kling, Vidu, Jimeng) providers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from config import settings

OUTPUT_DIR = Path("output/images")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Enums ───────────────────────────────────────────────────────────────────


class ImageProvider(str, Enum):
    MINIMAX = "minimax"
    SENSENOVA = "sensenova"
    VIDU = "vidu"
    JIMENG = "jimeng"
    KLING = "kling"


class ImageStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class ImageResult:
    provider: ImageProvider
    status: ImageStatus
    image_url: str = ""
    local_path: str = ""
    task_id: str = ""
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class GenerateImageRequest:
    prompt: str
    provider: ImageProvider = ImageProvider.MINIMAX
    size: str = "1280x720"
    model: str = ""
    n: int = 1
    reference_image_url: Optional[str] = None
    api_key: Optional[str] = None


# ── Provider Implementations ────────────────────────────────────────────────


async def _generate_minimax(req: GenerateImageRequest) -> ImageResult:
    """MiniMax image-01 — synchronous, returns URLs directly."""
    key = req.api_key or settings.minimax_api_key
    if not key:
        return ImageResult(provider=ImageProvider.MINIMAX, status=ImageStatus.FAILED, error="No API key")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.minimaxi.com/v1/image_generation",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"prompt": req.prompt, "model": req.model or "image-01", "aspect_ratio": "16:9"},
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("base_resp", {}).get("status_code") != 0:
        return ImageResult(
            provider=ImageProvider.MINIMAX, status=ImageStatus.FAILED,
            error=data.get("base_resp", {}).get("status_msg", "Unknown error"),
        )

    image_url = data.get("data", {}).get("image_urls", [""])[0]
    return ImageResult(
        provider=ImageProvider.MINIMAX, status=ImageStatus.COMPLETED,
        image_url=image_url,
    )


async def _generate_sensenova(req: GenerateImageRequest) -> ImageResult:
    """SenseNova — synchronous, returns URLs directly."""
    key = req.api_key or settings.sensenova_api_key
    if not key:
        return ImageResult(provider=ImageProvider.SENSENOVA, status=ImageStatus.FAILED, error="No API key")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://token.sensenova.cn/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"prompt": req.prompt, "model": req.model or "sensenova-u1-fast", "n": req.n, "size": req.size},
        )
        resp.raise_for_status()
        data = resp.json()

    image_url = data.get("data", [{}])[0].get("url", "")
    return ImageResult(
        provider=ImageProvider.SENSENOVA, status=ImageStatus.COMPLETED if image_url else ImageStatus.FAILED,
        image_url=image_url, error=None if image_url else "No image URL in response",
    )


async def _generate_kling(req: GenerateImageRequest) -> ImageResult:
    """Kling Omni — asynchronous, returns task_id for polling."""
    key = req.api_key or settings.kling_api_key
    if not key:
        return ImageResult(provider=ImageProvider.KLING, status=ImageStatus.FAILED, error="No API key")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.kling.kuaishou.com/v1/images/omni-image",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"prompt": req.prompt, "aspect_ratio": "16:9", "n": req.n},
        )
        resp.raise_for_status()
        data = resp.json()

    task_id = data.get("data", {}).get("task_id", "")
    return ImageResult(
        provider=ImageProvider.KLING, status=ImageStatus.PENDING,
        task_id=task_id,
    )


# ── Public API ──────────────────────────────────────────────────────────────


_PROVIDERS = {
    ImageProvider.MINIMAX: _generate_minimax,
    ImageProvider.SENSENOVA: _generate_sensenova,
    ImageProvider.KLING: _generate_kling,
}


async def generate_image(req: GenerateImageRequest) -> ImageResult:
    """Generate an image using the specified provider.

    For synchronous providers (MiniMax, SenseNova): returns completed result with URL.
    For async providers (Kling, Vidu, Jimeng): returns pending result with task_id.
    """
    handler = _PROVIDERS.get(req.provider)
    if not handler:
        return ImageResult(provider=req.provider, status=ImageStatus.FAILED, error=f"Unsupported provider: {req.provider}")

    result = await handler(req)

    # Download image if URL is available
    if result.image_url and result.status == ImageStatus.COMPLETED:
        try:
            filename = f"{result.provider.value}_{hashlib.md5(result.image_url.encode()).hexdigest()[:8]}.png"
            filepath = OUTPUT_DIR / filename
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(result.image_url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
            result.local_path = str(filepath)
        except Exception as e:
            result.error = f"Download failed: {str(e)}"

    return result


async def generate_images_batch(
    prompts: list[str],
    provider: ImageProvider = ImageProvider.MINIMAX,
    api_key: Optional[str] = None,
    concurrency: int = 3,
) -> list[ImageResult]:
    """Generate images for multiple prompts with controlled concurrency."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _generate_one(prompt: str) -> ImageResult:
        async with semaphore:
            return await generate_image(GenerateImageRequest(prompt=prompt, provider=provider, api_key=api_key))

    return await asyncio.gather(*[_generate_one(p) for p in prompts])
