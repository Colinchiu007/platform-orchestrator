"""Tests for slideshow — RED phase: write failing tests first, then implement."""

from __future__ import annotations

import os
import subprocess

import pytest

from services.story2video.slideshow import create_slideshow

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def image_factory(tmp_path):
    """Generate synthetic test images via ffmpeg color filter."""

    def _make(filename: str, color: str = "red", size: str = "640x480") -> str:
        path = str(tmp_path / filename)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c={color}:s={size}:d=1",
                "-frames:v", "1",
                path,
            ],
            capture_output=True, check=True,
        )
        return path

    return _make


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_video_duration(path: str) -> float:
    """Return video duration (seconds) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _get_video_resolution(path: str) -> tuple[int, int]:
    """Return (width, height) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True,
    )
    parts = result.stdout.strip().split(",")
    return int(parts[0]), int(parts[1])


def _get_video_framerate(path: str) -> float:
    """Return framerate via ffprobe (r_frame_rate)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True,
    )
    num, den = result.stdout.strip().split("/")
    return float(num) / float(den)


# ── Tests: Single Image ─────────────────────────────────────────────────────


class TestSingleImage:
    """Slideshow with one image — no transitions needed."""

    def test_single_image_produces_video(self, image_factory, tmp_path):
        """Single image should produce a valid MP4 with correct duration."""
        img = image_factory("single.png", color="red")
        output = str(tmp_path / "single.mp4")

        create_slideshow([(img, 3.0)], output)

        assert os.path.exists(output)
        duration = _get_video_duration(output)
        assert duration == pytest.approx(3.0, abs=0.2)

    def test_single_image_resolution(self, image_factory, tmp_path):
        """Default output resolution should be 1280x720."""
        img = image_factory("res.png", color="blue")
        output = str(tmp_path / "res.mp4")

        create_slideshow([(img, 2.0)], output)

        w, h = _get_video_resolution(output)
        assert (w, h) == (1280, 720)

    def test_single_image_framerate(self, image_factory, tmp_path):
        """Output framerate should match requested fps."""
        img = image_factory("fps.png", color="green")
        output = str(tmp_path / "fps.mp4")

        create_slideshow([(img, 2.0)], output, fps=24)

        fps = _get_video_framerate(output)
        assert fps == pytest.approx(24, abs=1)


# ── Tests: Multi-Image with Transitions ─────────────────────────────────────


class TestMultiImageTransitions:
    """Slideshow with multiple images and xfade crossfade transitions."""

    def test_two_images_produce_correct_duration(self, image_factory, tmp_path):
        """Two images of 3s each with 1s transition ≈ 5s total."""
        img1 = image_factory("t1a.png", color="red")
        img2 = image_factory("t1b.png", color="blue")
        output = str(tmp_path / "multi1.mp4")

        create_slideshow([(img1, 3.0), (img2, 3.0)], output)

        assert os.path.exists(output)
        duration = _get_video_duration(output)
        assert duration == pytest.approx(5.0, abs=0.3)

    def test_three_images_no_overlap(self, image_factory, tmp_path):
        """Three images of 2s each with 0.5s transition ≈ 5s total."""
        imgs = [
            image_factory("t2a.png", color="red"),
            image_factory("t2b.png", color="green"),
            image_factory("t2c.png", color="blue"),
        ]
        output = str(tmp_path / "multi2.mp4")

        create_slideshow(
            [(imgs[0], 2.0), (imgs[1], 2.0), (imgs[2], 2.0)],
            output,
            transition_duration=0.5,
        )

        assert os.path.exists(output)
        duration = _get_video_duration(output)
        assert duration == pytest.approx(5.0, abs=0.3)

    def test_different_durations(self, image_factory, tmp_path):
        """Images with varying durations should produce correct total."""
        imgs = [
            image_factory("t3a.png", color="white"),
            image_factory("t3b.png", color="black"),
        ]
        output = str(tmp_path / "multi3.mp4")

        create_slideshow([(imgs[0], 3.0), (imgs[1], 5.0)], output)

        assert os.path.exists(output)
        duration = _get_video_duration(output)
        assert duration == pytest.approx(7.0, abs=0.3)

    def test_custom_transition_duration(self, image_factory, tmp_path):
        """Custom transition duration should be reflected in total."""
        imgs = [
            image_factory("t4a.png", color="red"),
            image_factory("t4b.png", color="blue"),
        ]
        output = str(tmp_path / "multi4.mp4")

        create_slideshow(
            [(imgs[0], 4.0), (imgs[1], 4.0)],
            output,
            transition_duration=2.0,
        )

        assert os.path.exists(output)
        duration = _get_video_duration(output)
        assert duration == pytest.approx(6.0, abs=0.3)


# ── Tests: Output Properties ────────────────────────────────────────────────


class TestOutputProperties:
    """Video codec and container properties."""

    def test_output_resolution_two_images(self, image_factory, tmp_path):
        """Multi-image slideshow output should be 1280x720."""
        imgs = [
            image_factory("p1.png", color="red"),
            image_factory("p2.png", color="blue"),
        ]
        output = str(tmp_path / "props.mp4")

        create_slideshow([(imgs[0], 2.0), (imgs[1], 2.0)], output)

        w, h = _get_video_resolution(output)
        assert (w, h) == (1280, 720)


# ── Tests: Error Handling ───────────────────────────────────────────────────


class TestErrorHandling:
    """Input validation and error conditions."""

    def test_missing_image_raises_error(self, tmp_path):
        """Non-existent image file should raise FileNotFoundError."""
        output = str(tmp_path / "missing.mp4")
        with pytest.raises(FileNotFoundError):
            create_slideshow([("does_not_exist.png", 3.0)], output)

    def test_empty_images_list_raises_error(self, tmp_path):
        """Empty images list should raise ValueError."""
        output = str(tmp_path / "empty.mp4")
        with pytest.raises(ValueError):
            create_slideshow([], output)

    def test_zero_duration_raises_error(self, image_factory, tmp_path):
        """Image with zero duration should raise ValueError."""
        img = image_factory("zero.png", color="red")
        output = str(tmp_path / "zero.mp4")
        with pytest.raises(ValueError, match="positive"):
            create_slideshow([(img, 0.0)], output)
