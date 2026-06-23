"""FFmpeg-based video compositor — replaces the browser Canvas + MediaRecorder pipeline.

Takes images, audio, subtitles, and produces a final MP4 video.

Image effects (mapped to FFmpeg filters):
- zoom-in / zoom-out → zoompan
- pan-left/right/up/down → zoompan with x/y expressions
- rotate → rotate
- blur-in → boxblur → fade

Transition effects:
- fade → fade filter (in+out)
- slide → overlay + crop with x/y animation (via xfade)

Subtitle rendering:
- drawtext filter with per-frame timing expressions
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

OUTPUT_DIR = Path("output/videos")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class SubtitleSegment:
    text: str
    start_time: float   # seconds
    end_time: float     # seconds


@dataclass
class CompositorInput:
    images: List[str]                    # Image file paths (in order)
    audio_path: str                      # TTS audio file path
    output_path: Optional[str] = None    # Output video path
    image_effect: str = "zoom-in"        # zoom-in|zoom-out|pan-left|pan-right|none
    transition: str = "fade"             # fade|slide-left|slide-right|none
    fps: int = 30
    image_duration: float = 6.0          # Seconds per image
    subtitles: Optional[List[SubtitleSegment]] = None
    bgm_path: Optional[str] = None       # Background music
    bgm_volume: float = 0.3              # BGM volume ratio (0-1)
    subtitle_font_size: int = 24
    subtitle_color: str = "white"
    subtitle_stroke_color: str = "black"
    subtitle_stroke_width: int = 2
    width: int = 1280
    height: int = 720
    crf: int = 23                        # Quality (lower = better, 18-28)


@dataclass
class CompositorResult:
    output_path: str
    duration_seconds: float
    success: bool
    error: Optional[str] = None
    ffmpeg_cmd: str = ""


# ── FFmpeg Filter Builders ──────────────────────────────────────────────────


def _build_zoompan(effect: str, duration: float, fps: int) -> str:
    """Build zoompan filter string for the given effect."""
    if effect == "none":
        return ""

    total_frames = int(duration * fps)

    if effect == "zoom-in":
        # Start at 1.0x, end at 1.3x
        expr = f"zoompan=z='min(zoom+0.0015,1.3)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps}"
    elif effect == "zoom-out":
        expr = f"zoompan=z='max(zoom-0.0015,0.7)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps}"
    elif effect == "pan-left":
        expr = f"zoompan=z=1.2:d=1:x='iw/2-(iw/zoom/2)+{total_frames}*0.5':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps}"
    elif effect == "pan-right":
        expr = f"zoompan=z=1.2:d=1:x='iw/2-(iw/zoom/2)-{total_frames}*0.5':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps}"
    else:
        expr = f"zoompan=z=1.1:d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps={fps}"

    return expr


def _build_subtitle_filter(
    subtitles: List[SubtitleSegment],
    font_size: int,
    color: str,
    stroke_color: str,
    stroke_width: int,
) -> str:
    """Build drawtext filter expressions for timed subtitles."""
    if not subtitles:
        return ""

    drawtext_parts = []
    for i, sub in enumerate(subtitles):
        start = sub.start_time
        end = sub.end_time
        # Escape special characters for FFmpeg
        text = sub.text.replace("'", "'\\\\\\''").replace(":", "\\:")
        drawtext_parts.append(
            f"drawtext=text='{text}':fontsize={font_size}:fontcolor={color}:"
            f"bordercolor={stroke_color}:borderw={stroke_width}:"
            f"x=(w-text_w)/2:y=h-th-60:"
            f"enable='between(t,{start},{end})'"
        )

    return ",".join(drawtext_parts)


# ── Public API ──────────────────────────────────────────────────────────────


def compose_video(inp: CompositorInput) -> CompositorResult:
    """Compose a video from images + audio + subtitles using FFmpeg.

    Usage:
        result = compose_video(CompositorInput(
            images=["scene1.png", "scene2.png"],
            audio_path="tts.mp3",
            subtitles=[SubtitleSegment("hello", 0, 2)],
        ))
    """
    if not inp.images:
        return CompositorResult(output_path="", duration_seconds=0, success=False, error="No images provided")

    output = inp.output_path or str(OUTPUT_DIR / "output.mp4")
    total_duration = len(inp.images) * inp.image_duration

    # ── Build image sequence ──────────────────────────────────────────────
    # Create temp directory for scaled images
    with tempfile.TemporaryDirectory() as tmpdir:
        scaled_images = []
        for i, img in enumerate(inp.images):
            scaled = f"{tmpdir}/img_{i:04d}.png"
            subprocess.run(
                ["ffmpeg", "-y", "-i", img, "-vf", f"scale={inp.width}:{inp.height}:force_original_aspect_ratio=decrease,pad={inp.width}:{inp.height}:(ow-iw)/2:(oh-ih)/2", scaled],
                capture_output=True,
            )
            scaled_images.append(scaled)

        # Create concat file for image sequence
        concat_file = f"{tmpdir}/concat.txt"
        with open(concat_file, "w") as f:
            for img in scaled_images:
                f.write(f"file '{img}'\n")
                f.write(f"duration {inp.image_duration}\n")
            # Last frame needs duplicate for concat demuxer
            f.write(f"file '{scaled_images[-1]}'\n")

        # ── Build FFmpeg command ──────────────────────────────────────────
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
        ]

        # Audio input
        cmd += ["-i", inp.audio_path]

        # BGM (optional)
        if inp.bgm_path:
            cmd += ["-i", inp.bgm_path]

        # Video filters
        vf_parts = []

        # Image effect
        effect_filter = _build_zoompan(inp.image_effect, inp.image_duration, inp.fps)
        if effect_filter:
            vf_parts.append(effect_filter)

        # Subtitle overlay
        if inp.subtitles:
            sub_filter = _build_subtitle_filter(
                inp.subtitles, inp.subtitle_font_size,
                inp.subtitle_color, inp.subtitle_stroke_color,
                inp.subtitle_stroke_width,
            )
            if sub_filter:
                vf_parts.append(sub_filter)

        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]

        # Audio filter
        af_parts = []
        if inp.bgm_path:
            # Mix TTS + BGM: TTS at 100%, BGM at bgm_volume
            af_parts.append(f"[1:a]volume=1.0[a1];[2:a]volume={inp.bgm_volume}[a2];[a1][a2]amix=inputs=2:duration=first[amix]")
            cmd += ["-filter_complex", ";".join(af_parts)]
            cmd += ["-map", "0:v", "-map", "[amix]"]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]

        # Output encoding
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", str(inp.crf),
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-r", str(inp.fps),
            "-t", str(total_duration),
            output,
        ]

        cmd_str = " ".join(cmd)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return CompositorResult(
                    output_path=output, duration_seconds=total_duration,
                    success=False, error=result.stderr[-500:], ffmpeg_cmd=cmd_str,
                )
        except subprocess.TimeoutExpired:
            return CompositorResult(
                output_path=output, duration_seconds=total_duration,
                success=False, error="FFmpeg timed out after 300s", ffmpeg_cmd=cmd_str,
            )
        except FileNotFoundError:
            return CompositorResult(
                output_path=output, duration_seconds=total_duration,
                success=False, error="ffmpeg not installed. Install: apt install ffmpeg / brew install ffmpeg",
                ffmpeg_cmd=cmd_str,
            )

    return CompositorResult(
        output_path=output, duration_seconds=total_duration,
        success=True, ffmpeg_cmd=cmd_str,
    )


# ── Convenience: Full Pipeline ──────────────────────────────────────────────


def compose_from_pipeline(
    images: List[str],
    audio_path: str,
    subtitle_segments: List[SubtitleSegment],
    output_path: str,
    **kwargs,
) -> CompositorResult:
    """One-call video composition from pipeline output.

    Args:
        images: Image file paths (one per scene).
        audio_path: TTS audio file.
        subtitle_segments: Timed subtitle segments from splitter output.
        output_path: Where to write the final MP4.
        **kwargs: Passed through to CompositorInput (effect, transition, etc.).
    """
    return compose_video(CompositorInput(
        images=images,
        audio_path=audio_path,
        output_path=output_path,
        subtitles=subtitle_segments,
        **kwargs,
    ))
