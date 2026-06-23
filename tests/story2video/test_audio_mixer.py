"""Tests for audio_mixer — RED phase: write failing tests first, then implement."""

from __future__ import annotations

import os
import subprocess

import pytest

from services.story2video.audio_mixer import mix_audio_clips


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sine_clip_factory(tmp_path):
    """Generate synthetic sine-wave WAV files for testing via ffmpeg."""

    def _make(filename: str, frequency: int = 440, duration: float = 1.0) -> str:
        path = str(tmp_path / filename)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"sine=frequency={frequency}:duration={duration}",
                "-ac", "1", "-ar", "22050",
                path,
            ],
            capture_output=True, check=True,
        )
        return path

    return _make


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_duration(path: str) -> float:
    """Return duration (seconds) of an audio file via ffprobe."""
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


# ── Tests ───────────────────────────────────────────────────────────────────


class TestBasicMixing:
    """Fundamental mixing behaviour."""

    def test_two_clips_produce_sum_duration(self, sine_clip_factory, tmp_path):
        """Two clips of 1s + 1.5s should produce ~2.5s output."""
        clip1 = sine_clip_factory("clip1.wav", duration=1.0)
        clip2 = sine_clip_factory("clip2.wav", duration=1.5)
        output = str(tmp_path / "mixed.wav")

        mix_audio_clips([clip1, clip2], output)

        assert os.path.exists(output)
        duration = _get_duration(output)
        assert duration == pytest.approx(2.5, abs=0.15)

    def test_single_clip_passthrough(self, sine_clip_factory, tmp_path):
        """Single clip should produce output with same duration."""
        clip = sine_clip_factory("single.wav", duration=2.0)
        output = str(tmp_path / "single_out.wav")

        mix_audio_clips([clip], output)

        assert os.path.exists(output)
        duration = _get_duration(output)
        assert duration == pytest.approx(2.0, abs=0.1)


class TestFadeEffects:
    """Fade-in and fade-out effects."""

    def test_fade_in_out_applied(self, sine_clip_factory, tmp_path):
        """Output should exist and have expected duration with fades."""
        clip = sine_clip_factory("fade_clip.wav", duration=3.0)
        output = str(tmp_path / "faded.wav")

        mix_audio_clips([clip], output, fade_in=0.5, fade_out=1.0)

        assert os.path.exists(output)
        duration = _get_duration(output)
        # Duration should still be ~3s (fades don't change length)
        assert duration == pytest.approx(3.0, abs=0.1)

    def test_custom_fade_durations(self, sine_clip_factory, tmp_path):
        """Non-default fade values should not break output."""
        clip = sine_clip_factory("src_fade.wav", duration=4.0)
        output = str(tmp_path / "custom_fade.wav")

        mix_audio_clips([clip], output, fade_in=0.1, fade_out=2.0)

        assert os.path.exists(output)
        duration = _get_duration(output)
        assert duration == pytest.approx(4.0, abs=0.1)


class TestBgmOverlay:
    """Background music overlay."""

    def test_bgm_overlay_creates_output(self, sine_clip_factory, tmp_path):
        """BGM overlay should produce valid output with correct duration."""
        clip = sine_clip_factory("voice.wav", duration=2.0)
        bgm = sine_clip_factory("bgm.wav", frequency=220, duration=5.0)
        output = str(tmp_path / "with_bgm.wav")

        mix_audio_clips([clip], output, bgm_path=bgm)

        assert os.path.exists(output)
        # Duration should match the main clip, not the longer BGM
        duration = _get_duration(output)
        assert duration == pytest.approx(2.0, abs=0.1)

    def test_bgm_shorter_than_clips(self, sine_clip_factory, tmp_path):
        """Short BGM should loop to fill the full clip duration."""
        clip = sine_clip_factory("long_voice.wav", duration=4.0)
        bgm = sine_clip_factory("short_bgm.wav", frequency=220, duration=1.0)
        output = str(tmp_path / "bgm_loop.wav")

        mix_audio_clips([clip], output, bgm_path=bgm)

        assert os.path.exists(output)
        duration = _get_duration(output)
        assert duration == pytest.approx(4.0, abs=0.15)


class TestVolume:
    """Volume adjustment."""

    def test_volume_adjustment(self, sine_clip_factory, tmp_path):
        """Volume > 1.0 should amplify (output still valid)."""
        clip = sine_clip_factory("quiet.wav", duration=1.0)
        output = str(tmp_path / "loud.wav")

        mix_audio_clips([clip], output, volume=2.0)

        assert os.path.exists(output)
        duration = _get_duration(output)
        assert duration == pytest.approx(1.0, abs=0.1)


class TestErrorHandling:
    """Input validation and error handling."""

    def test_missing_clip_file_raises_error(self, tmp_path):
        """Non-existent clip file should raise FileNotFoundError."""
        output = str(tmp_path / "no_output.wav")
        with pytest.raises(FileNotFoundError):
            mix_audio_clips(["nonexistent.wav"], output)

    def test_empty_clips_list_raises_error(self, tmp_path):
        """Empty clips list should raise ValueError."""
        output = str(tmp_path / "empty_out.wav")
        with pytest.raises(ValueError):
            mix_audio_clips([], output)

    def test_missing_bgm_file_raises_error(self, sine_clip_factory, tmp_path):
        """Non-existent BGM file should raise FileNotFoundError."""
        clip = sine_clip_factory("ok.wav", duration=1.0)
        output = str(tmp_path / "no_bgm_out.wav")
        with pytest.raises(FileNotFoundError):
            mix_audio_clips([clip], output, bgm_path="no_bgm.wav")
