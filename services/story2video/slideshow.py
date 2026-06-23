"""Slideshow compositing — Ken Burns zoom/pan + crossfade transitions via FFmpeg.

Generates an MP4 video from a sequence of still images, applying a gentle
zoom-in (Ken Burns) effect on each image and crossfading between them.

Usage::

    from services.story2video.slideshow import create_slideshow

    create_slideshow(
        [("scene1.png", 5.0), ("scene2.png", 3.0)],
        "output.mp4",
        fps=24,
        transition_duration=1.0,
    )
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Tuple


# ── Constants ───────────────────────────────────────────────────────────────

OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
ZOOM_MAX = 1.3
ZOOM_SPEED = 0.0015
TRANSITION_DURATION = 1.0
FPS = 24


# ── Public API ──────────────────────────────────────────────────────────────


def create_slideshow(
    images: List[Tuple[str, float]],
    output_path: str,
    fps: int = FPS,
    transition_duration: float = TRANSITION_DURATION,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
    zoom_max: float = ZOOM_MAX,
    zoom_speed: float = ZOOM_SPEED,
) -> str:
    """Create a video slideshow with Ken Burns effect and crossfade transitions.

    Each image is shown as a video segment with a slow zoom-in (Ken Burns).
    Consecutive segments are blended via the FFmpeg ``xfade`` filter.

    Args:
        images: List of ``(image_path, duration_seconds)`` tuples.
        output_path: Destination MP4 file path.
        fps: Output framerate (default: 24).
        transition_duration: Crossfade overlap in seconds (default: 1.0).
        width: Output video width in pixels (default: 1280).
        height: Output video height in pixels (default: 720).
        zoom_max: Maximum zoom factor for the Ken Burns effect (default: 1.3).
        zoom_speed: Per-frame zoom increment (default: 0.0015).

    Returns:
        ``output_path`` on success.

    Raises:
        ValueError: If ``images`` is empty or any duration is non-positive.
        FileNotFoundError: If any image file does not exist.
        RuntimeError: If FFmpeg or ffprobe fails.
    """
    if not images:
        raise ValueError("images list cannot be empty")

    durations: List[float] = []
    for img_path, duration in images:
        if duration <= 0:
            raise ValueError(
                f"duration must be positive, got {duration} for {img_path}"
            )
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"Image file not found: {img_path}")
        durations.append(duration)

    n = len(images)

    # ── Single image — simple zoompan, no transitions ────────────────
    if n == 1:
        _run_single_image(
            images[0][0], durations[0], output_path, fps, width, height,
            zoom_max, zoom_speed,
        )
        return output_path

    # ── Multiple images — zoompan each input + chained xfade ─────────
    _run_multi_image(
        images, durations, output_path, fps, width, height,
        zoom_max, zoom_speed, transition_duration,
    )
    return output_path


# ── Internals ───────────────────────────────────────────────────────────────


def _build_zoompan_filter(
    label: str,
    width: int,
    height: int,
    fps: int,
    zoom_max: float,
    zoom_speed: float,
) -> str:
    """Build a zoompan filter expression for a single input stream.

    Applies a smooth Ken-Burns zoom-in from 1.0× to *zoom_max*× over the
    duration of the input, and scales the output to ``width × height``.
    """
    return (
        f"[{label}:v]zoompan="
        f"z='min(zoom+{zoom_speed},{zoom_max})':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps}"
        f"[v{label}]"
    )


def _build_xfade_filter(
    prev_label: str,
    curr_label: str,
    out_label: str,
    offset: float,
    transition_duration: float,
) -> str:
    """Build an xfade transition between two video streams."""
    return (
        f"[v{prev_label}][v{curr_label}]"
        f"xfade=transition=fade:"
        f"duration={transition_duration}:"
        f"offset={offset}"
        f"[v{out_label}]"
    )


def _run_single_image(
    img_path: str,
    duration: float,
    output_path: str,
    fps: int,
    width: int,
    height: int,
    zoom_max: float,
    zoom_speed: float,
) -> None:
    """Render one image with zoompan, no transitions."""
    zoompan_expr = (
        f"zoompan=z='min(zoom+{zoom_speed},{zoom_max})':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps), "-loop", "1", "-t", str(duration),
        "-i", img_path,
        "-vf", zoompan_expr,
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        output_path,
    ]
    _run_ffmpeg(cmd)


def _run_multi_image(
    images: List[Tuple[str, float]],
    durations: List[float],
    output_path: str,
    fps: int,
    width: int,
    height: int,
    zoom_max: float,
    zoom_speed: float,
    transition_duration: float,
) -> None:
    """Render multiple images with zoompan and chained xfade transitions."""
    n = len(images)

    # ── Input arguments ──────────────────────────────────────────────
    inputs: List[str] = []
    for img_path, img_duration in images:
        inputs.extend([
            "-framerate", str(fps), "-loop", "1", "-t", str(img_duration),
            "-i", img_path,
        ])

    # ── Filter graph ─────────────────────────────────────────────────
    filter_parts: List[str] = []

    # Zoompan for each input
    for i in range(n):
        filter_parts.append(
            _build_zoompan_filter(str(i), width, height, fps, zoom_max, zoom_speed)
        )

    # Chained xfade transitions.
    # After each xfade the running duration shrinks by transition_duration.
    running = durations[0]
    for i in range(1, n):
        offset = running - transition_duration
        filter_parts.append(
            _build_xfade_filter(str(i - 1), str(i), str(i), offset, transition_duration)
        )
        running = running + durations[i] - transition_duration

    total_duration = running

    filter_str = "; ".join(filter_parts)
    last_label = str(n - 1)

    cmd = ["ffmpeg", "-y"]
    cmd.extend(inputs)
    cmd.extend(["-filter_complex", filter_str])
    cmd.extend(["-map", f"[v{last_label}]"])
    cmd.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-t", str(total_duration),
        output_path,
    ])
    _run_ffmpeg(cmd)


def _run_ffmpeg(cmd: List[str]) -> None:
    """Execute an FFmpeg command and handle common errors."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg executable not found. "
            "Install: apt install ffmpeg / brew install ffmpeg"
        ) from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"FFmpeg slideshow failed:\n{exc.stderr[-500:]}"
        ) from exc
