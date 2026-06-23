"""Story2Video pipeline orchestrator — full text→video pipeline.

Pipeline::
    article_text → segment_text → scenes
        → per-scene TTS (or fallback silence)
        → mix_audio_clips → mixed_audio.mp3
        → per-scene placeholder images (Pillow)
        → create_slideshow → slideshow.mp4
        → mux slideshow + audio → final.mp4

Usage::
    result = run_story2video_pipeline(
        article_text="Hello world.",
        output_dir="/tmp/story2video",
    )
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from services.compositor import CompositorInput, CompositorResult, compose_video
from services.story2video.audio_mixer import mix_audio_clips
from services.story2video.slideshow import create_slideshow
from services.story2video.text_segmentation import segment_text
from services.tts_service import text_to_speech

# ── Constants ────────────────────────────────────────────────────────────────

SLIDE_WIDTH = 1280
SLIDE_HEIGHT = 720
PLACEHOLDER_COLORS: list[tuple[int, int, int]] = [
    (52, 152, 219),  # blue
    (46, 204, 113),  # green
    (155, 89, 182),  # purple
    (231, 76, 60),   # red
    (243, 156, 18),  # orange
    (26, 188, 156),  # teal
    (52, 73, 94),    # dark
    (230, 126, 34),  # dark orange
    (41, 128, 185),  # lighter blue
    (39, 174, 96),   # lighter green
]
SILENT_DURATION_PER_CHAR = 0.05  # seconds per char when generating silent audio
OUTPUT_VIDEO_FPS = 24


# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Result of running the full Story2Video pipeline."""

    success: bool
    output_path: Optional[str] = None
    scenes: int = 0
    images_generated: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


# ── Private Helpers ──────────────────────────────────────────────────────────


def _generate_placeholder_image(
    index: int,
    scene_text: str,
    output_dir: str,
    width: int = SLIDE_WIDTH,
    height: int = SLIDE_HEIGHT,
) -> str:
    """Create a solid-color placeholder image with scene number.

    Uses a rotating color palette. Renders the scene number and a snippet
    of text onto the image.
    """
    color = PLACEHOLDER_COLORS[index % len(PLACEHOLDER_COLORS)]
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)

    # Draw scene number
    draw.text(
        (40, 40),
        f"Scene {index + 1}",
        fill="white",
        stroke_width=2,
        stroke_color="black",
    )

    # Draw first ~80 chars of scene text as preview
    preview = scene_text[:80].strip()
    if len(scene_text) > 80:
        preview += "…"
    if preview:
        draw.text(
            (40, 100),
            preview,
            fill="white",
            stroke_width=1,
            stroke_color="black",
        )

    filepath = os.path.join(output_dir, f"scene_{index:04d}.png")
    img.save(filepath, "PNG")
    return filepath


def _generate_silent_audio(duration: float, output_path: str) -> bool:
    """Generate a silent audio file of *duration* seconds via FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=mono",
        "-t", str(duration),
        "-c:a", "aac",
        "-b:a", "128k",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _mux_video_audio(
    video_path: str,
    audio_path: str,
    output_path: str,
) -> bool:
    """Mux a video stream with an audio track (copy video, encode audio).

    Follows the FFmpeg pattern established in services/compositor.py.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# ── Public API ───────────────────────────────────────────────────────────────


async def run_story2video_pipeline(
    article_text: str,
    output_dir: str,
    voice_id: str = "zh_female_qingxinnvsheng_uranus_bigtts",
    image_effect: str = "zoom-in",
    transition: str = "fade",
    fps: int = OUTPUT_VIDEO_FPS,
    max_scene_duration: float = 30.0,
) -> PipelineResult:
    """Run the full Story2Video pipeline from text to final MP4.

    Args:
        article_text: The source text to convert to video.
        output_dir: Directory for all intermediate and final artifacts.
        voice_id: TTS voice ID for speech synthesis.
        image_effect: Image effect for compositor (zoom-in, zoom-out, etc.).
        transition: Transition effect (fade, slide, etc.).
        fps: Output video framerate.
        max_scene_duration: Max seconds per scene (segment_text param).

    Returns:
        PipelineResult with output path and summary statistics.
    """
    artifact_dir = Path(output_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Segment text into scenes ──────────────────────────────────────
    scenes = segment_text(article_text, max_scene_duration=max_scene_duration)
    if not scenes:
        return PipelineResult(success=True, scenes=0)

    # ── 2. Generate TTS audio per scene (or silent fallback) ────────────
    audio_clips: list[str] = []
    for scene in scenes:
        tts_result = await text_to_speech(
            text=scene.text,
            voice_id=voice_id,
        )

        if tts_result.error or not tts_result.audio_path:
            # TTS failed — fall back to silent audio
            scene_duration = scene.end_time - scene.start_time
            if scene_duration <= 0:
                scene_duration = len(scene.text) * SILENT_DURATION_PER_CHAR
            silent_path = os.path.join(output_dir, f"silent_{len(audio_clips):04d}.aac")
            if _generate_silent_audio(scene_duration, silent_path):
                audio_clips.append(silent_path)
            else:
                # If even silent generation fails, skip this scene's audio
                continue
        else:
            audio_clips.append(tts_result.audio_path)

    # ── 3. Mix audio clips into a single track ──────────────────────────
    mixed_audio = os.path.join(output_dir, "mixed_audio.mp3")
    if audio_clips:
        try:
            mix_audio_clips(clips=audio_clips, output_path=mixed_audio)
        except (ValueError, FileNotFoundError, RuntimeError):
            # If mixing fails, try to use the first audio clip directly
            mixed_audio = audio_clips[0]

    # ── 4. Generate placeholder images ───────────────────────────────────
    image_paths: list[str] = []
    for i, scene in enumerate(scenes):
        img_path = _generate_placeholder_image(i, scene.text, output_dir)
        image_paths.append(img_path)

    # ── 5. Create slideshow video from images ───────────────────────────
    scene_durations = [s.end_time - s.start_time for s in scenes]
    slideshow_path = os.path.join(output_dir, "slideshow.mp4")

    try:
        create_slideshow(
            images=list(zip(image_paths, scene_durations)),
            output_path=slideshow_path,
            fps=fps,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return PipelineResult(
            success=False,
            scenes=len(scenes),
            images_generated=len(image_paths),
            error=f"Slideshow creation failed: {exc}",
        )

    # ── 6. Combine slideshow video + mixed audio into final MP4 ─────────
    final_path = os.path.join(output_dir, "final.mp4")

    if audio_clips and os.path.exists(mixed_audio):
        # Use the compositor with placeholder images + mixed audio
        composit_result: CompositorResult = compose_video(CompositorInput(
            images=image_paths,
            audio_path=mixed_audio,
            output_path=final_path,
            image_effect=image_effect,
            transition=transition,
            fps=fps,
            image_duration=max(scene_durations) if scene_durations else 5.0,
        ))

        if not composit_result.success:
            # Fall back to muxing the slideshow video with audio
            if not _mux_video_audio(slideshow_path, mixed_audio, final_path):
                # If everything fails, return the slideshow as-is
                final_path = slideshow_path
    else:
        # No audio — use the slideshow directly
        final_path = slideshow_path

    return PipelineResult(
        success=True,
        output_path=final_path,
        scenes=len(scenes),
        images_generated=len(image_paths),
        duration_seconds=sum(scene_durations),
    )
