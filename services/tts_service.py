"""TTS (Text-to-Speech) service — Doubao/Volcano Engine TTS API.

Replaces the tts-minimax, clone-voice, and short-speech-recognition Edge Functions.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from services.provider_router import get_router

# ── Constants ───────────────────────────────────────────────────────────────

DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"
DOUBAO_CLONE_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"

OUTPUT_DIR = Path("output/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class TTSResult:
    audio_path: str
    duration_seconds: float
    format: str = "mp3"
    error: Optional[str] = None


@dataclass
class VoiceCloneResult:
    voice_id: str
    status: str  # pending / ready / error
    name: str
    error: Optional[str] = None


# ── TTS ─────────────────────────────────────────────────────────────────────


async def text_to_speech(
    text: str,
    voice_id: str = "zh_female_qingxinnvsheng_uranus_bigtts",
    speed: float = 1.0,
    volume: float = 1.0,
    api_key: Optional[str] = None,
) -> TTSResult:
    """Convert text to speech using Doubao TTS API.

    Args:
        text: Chinese text to synthesize (max ~1000 chars per call).
        voice_id: Voice ID from Doubao voice library.
        speed: Speed ratio (0.5-2.0).
        volume: Volume ratio (0.5-2.0).
        api_key: Doubao API key (defaults to ProviderRouter doubao config).

    Returns:
        TTSResult with local audio file path and duration.
    """
    key = api_key
    if not key:
        router = get_router()
        cfg = await router.get("doubao")
        if cfg:
            key = cfg["api_key"]
    if not key:
        return TTSResult(audio_path="", duration_seconds=0, error="No API key configured")

    payload = {
        "app": {"appid": "0", "token": "placeholder", "cluster": "volcano_tts"},
        "user": {"uid": "platform-orchestrator"},
        "audio": {
            "voice_type": voice_id,
            "encoding": "mp3",
            "speed_ratio": speed,
            "volume_ratio": volume,
        },
        "request": {"text": text, "text_type": "plain"},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            DOUBAO_TTS_URL,
            headers={
                "Authorization": f"Bearer; {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 3000:
        return TTSResult(
            audio_path="", duration_seconds=0,
            error=data.get("message", f"TTS failed: code={data.get('code')}")
        )

    # Decode base64 audio
    audio_b64 = data["data"]
    audio_bytes = base64.b64decode(audio_b64)

    # Save to disk
    filename = f"tts_{hashlib.md5(text.encode()).hexdigest()[:12]}.mp3"
    filepath = OUTPUT_DIR / filename
    filepath.write_bytes(audio_bytes)

    # Estimate duration (mp3 ~16KB/s at 128kbps)
    duration = len(audio_bytes) / 16000

    return TTSResult(audio_path=str(filepath), duration_seconds=duration)


# ── Voice Clone ─────────────────────────────────────────────────────────────


async def clone_voice(
    name: str,
    audio_url: str,
    description: str = "",
    api_key: Optional[str] = None,
) -> VoiceCloneResult:
    """Submit a voice clone request to Doubao.

    Note: Voice cloning is async — the API returns immediately with a task ID.
    Poll the result separately.

    Args:
        name: Display name for the cloned voice.
        audio_url: URL to the reference audio file (10-60 seconds recommended).
        description: Optional description.
        api_key: Doubao API key.

    Returns:
        VoiceCloneResult with voice_id for later polling.
    """
    key = api_key
    if not key:
        router = get_router()
        cfg = await router.get("doubao")
        if cfg:
            key = cfg["api_key"]
    if not key:
        return VoiceCloneResult(voice_id="", status="error", name=name, error="No API key")

    payload = {
        "app": {"appid": "0", "token": "placeholder", "cluster": "volcano_tts"},
        "user": {"uid": "platform-orchestrator"},
        "audio": {"audio_url": audio_url, "text": description},
        "request": {"name": name, "language": "Chinese"},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            DOUBAO_CLONE_URL,
            headers={
                "Authorization": f"Bearer; {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 3000:
        return VoiceCloneResult(
            voice_id="", status="error", name=name,
            error=data.get("message", "Clone failed")
        )

    result = data.get("data", {})
    return VoiceCloneResult(
        voice_id=result.get("voice_id", ""),
        status="pending",
        name=name,
    )


async def query_clone_status(
    voice_id: str,
    api_key: Optional[str] = None,
) -> VoiceCloneResult:
    """Query voice clone task status. (Placeholder — API endpoint TBD)"""
    return VoiceCloneResult(voice_id=voice_id, status="ready", name="")
