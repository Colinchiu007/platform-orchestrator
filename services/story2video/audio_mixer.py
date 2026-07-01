"""Audio mixing module for Story2Video.

FFmpeg-based clip concatenation and BGM overlay.

Uses subprocess.run(["ffmpeg", ...]) following the pattern established in
services/compositor.py.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

BGM_VOLUME = 0.3


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_duration(path: str) -> float:
    """Return audio duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Cannot determine duration for: {path}")
    return float(result.stdout.strip())


# ── Public API ──────────────────────────────────────────────────────────────


def mix_audio_clips(
    clips: List[str],
    output_path: str,
    bgm_path: Optional[str] = None,
    fade_in: float = 0.5,
    fade_out: float = 1.0,
    volume: float = 1.0,
) -> None:
    """Concatenate audio clips with fades and optional BGM overlay.

    Args:
        clips: List of audio file paths to concatenate.
        output_path: Output audio file path.
        bgm_path: Optional background music file (loops if shorter than clips).
        fade_in: Fade-in duration in seconds.
        fade_out: Fade-out duration in seconds.
        volume: Overall volume adjustment (1.0 = no change).

    Raises:
        ValueError: If *clips* is empty.
        FileNotFoundError: If any input file does not exist.
        RuntimeError: If FFmpeg or ffprobe fails.
    """
    # ── Validate inputs ─────────────────────────────────────────────────
    if not clips:
        raise ValueError("clips list cannot be empty")

    for clip in clips:
        if not os.path.exists(clip):
            raise FileNotFoundError(f"Clip file not found: {clip}")
    if bgm_path and not os.path.exists(bgm_path):
        raise FileNotFoundError(f"BGM file not found: {bgm_path}")

    # ── Compute total duration ─────────────────────────────────────────
    total_duration = sum(_get_duration(c) for c in clips)

    n = len(clips)
    cmd = ["ffmpeg", "-y"]

    # ── Inputs ─────────────────────────────────────────────────────────
    for clip in clips:
        cmd += ["-i", clip]

    parts: List[str] = []

    # ── 1. Concatenate all clip audio streams ──────────────────────────
    concat_labels = "".join(f"[{i}:a]" for i in range(n))
    parts.append(f"{concat_labels}concat=n={n}:v=0:a=1[concated]")

    if bgm_path:
        # BGM input — loop infinitely, trim to total_duration below
        cmd += ["-stream_loop", "-1", "-i", bgm_path]

        # Trim BGM & reduce volume
        parts.append(
            f"[{n}:a]atrim=end={total_duration},volume={BGM_VOLUME}[bgm]"
        )
        # Mix concat'd audio + BGM
        parts.append("[concated][bgm]amix=inputs=2:duration=first[mixed]")
        fade_source = "[mixed]"
    else:
        fade_source = "[concated]"

    # ── 2. Apply fades + volume ────────────────────────────────────────
    fade_start = max(0.0, total_duration - fade_out)
    chain = (
        f"{fade_source}afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={fade_start}:d={fade_out}"
    )
    if volume != 1.0:
        chain += f",volume={volume}"
    chain += "[aout]"
    parts.append(chain)

    cmd += ["-filter_complex", "; ".join(parts)]
    cmd += ["-map", "[aout]", output_path]

    # ── Execute ────────────────────────────────────────────────────────
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg executable not found. Ensure it is installed and in PATH."
        ) from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"FFmpeg audio mixing failed:\n{exc.stderr[-500:]}"
        ) from exc
