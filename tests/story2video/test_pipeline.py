"""Tests for Story2Video pipeline orchestrator.

TDD: RED phase — write failing tests first, then implement.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.story2video.pipeline import PipelineResult, run_story2video_pipeline

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_tts_result(audio_path: str, duration: float = 1.5, error: str | None = None):
    """Create a mock TTSResult-like object."""
    return MagicMock(audio_path=audio_path, duration_seconds=duration, error=error)


def _make_composit_result(success: bool = True, output_path: str = "/tmp/final.mp4",
                          duration: float = 4.5, error: str | None = None):
    """Create a mock CompositorResult-like object."""
    return MagicMock(
        success=success,
        output_path=output_path,
        duration_seconds=duration,
        error=error,
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def output_dir() -> str:
    """Provide a temporary output directory for pipeline artifacts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ── Pipeline: run_story2video_pipeline ───────────────────────────────────────


class TestRunStory2VideoPipeline:
    """Integration-style tests for the full pipeline orchestrator.

    External calls (TTS, FFmpeg) are mocked; image generation with Pillow
    runs for real to produce placeholder images.
    """
    pytestmark = pytest.mark.asyncio
    """Integration-style tests for the full pipeline orchestrator.

    External calls (TTS, FFmpeg) are mocked; image generation with Pillow
    runs for real to produce placeholder images.
    """

    @patch("services.story2video.pipeline.text_to_speech", new_callable=AsyncMock)
    @patch("services.story2video.pipeline.mix_audio_clips")
    @patch("services.story2video.pipeline.create_slideshow")
    @patch("services.story2video.pipeline.compose_video")
    @patch("services.story2video.pipeline._generate_silent_audio")
    async def test_pipeline_with_small_text_produces_output_structure(
        self,
        mock_silent: MagicMock,
        mock_compose: MagicMock,
        mock_slideshow: MagicMock,
        mock_mixer: MagicMock,
        mock_tts: AsyncMock,
        output_dir: str,
    ):
        """Pipeline with a few sentences produces scenes, images, output_path."""
        # ── Arrange ──────────────────────────────────────────────────────
        text = "The quick brown fox. Jumps over the lazy dog. Goodbye."

        # Pre-create the mixed audio file so the file-exists check passes
        mixed_audio = os.path.join(output_dir, "mixed_audio.mp3")
        Path(mixed_audio).write_text("fake mixed audio")

        audio_path = os.path.join(output_dir, "tts_test.mp3")
        Path(audio_path).write_text("fake audio bytes")
        mock_tts.return_value = _make_tts_result(audio_path)

        mock_slideshow.return_value = os.path.join(output_dir, "slideshow.mp4")
        mock_compose.return_value = _make_composit_result(
            output_path=os.path.join(output_dir, "final.mp4"),
        )

        # ── Act ──────────────────────────────────────────────────────────
        result = await run_story2video_pipeline(
            article_text=text,
            output_dir=output_dir,
            max_scene_duration=2.0,
        )

        # ── Assert ───────────────────────────────────────────────────────
        assert result.success is True
        assert result.scenes > 0
        assert result.output_path is not None
        assert result.output_path != ""

        # Should have generated images (one per scene)
        assert result.images_generated > 0
        assert result.images_generated == result.scenes

        # Verify mocks were called
        assert mock_tts.call_count >= 1
        assert mock_mixer.called
        assert mock_slideshow.called
        assert mock_compose.called
        # Silent fallback should NOT be called when TTS succeeds
        mock_silent.assert_not_called()

    @patch("services.story2video.pipeline.text_to_speech", new_callable=AsyncMock)
    @patch("services.story2video.pipeline.mix_audio_clips")
    @patch("services.story2video.pipeline.create_slideshow")
    @patch("services.story2video.pipeline.compose_video")
    @patch("services.story2video.pipeline._generate_silent_audio")
    async def test_empty_text_returns_early(
        self,
        mock_silent: MagicMock,
        mock_compose: MagicMock,
        mock_slideshow: MagicMock,
        mock_mixer: MagicMock,
        mock_tts: AsyncMock,
        output_dir: str,
    ):
        """Empty text returns early with no scenes and no external calls."""
        # ── Act ──────────────────────────────────────────────────────────
        result = await run_story2video_pipeline(
            article_text="",
            output_dir=output_dir,
        )

        # ── Assert ───────────────────────────────────────────────────────
        assert result.success is True
        assert result.scenes == 0
        assert result.images_generated == 0
        assert result.output_path is None

        mock_tts.assert_not_called()
        mock_mixer.assert_not_called()
        mock_slideshow.assert_not_called()
        mock_compose.assert_not_called()
        mock_silent.assert_not_called()

    @patch("services.story2video.pipeline.text_to_speech", new_callable=AsyncMock)
    @patch("services.story2video.pipeline.mix_audio_clips")
    @patch("services.story2video.pipeline.create_slideshow")
    @patch("services.story2video.pipeline.compose_video")
    @patch("services.story2video.pipeline._generate_silent_audio")
    async def test_pipeline_with_cjk_text(
        self,
        mock_silent: MagicMock,
        mock_compose: MagicMock,
        mock_slideshow: MagicMock,
        mock_mixer: MagicMock,
        mock_tts: AsyncMock,
        output_dir: str,
    ):
        """CJK text is handled correctly by the pipeline."""
        # ── Arrange ──────────────────────────────────────────────────────
        text = "今天天气真好。我们一起去公园吧。明天见。"

        mixed_audio = os.path.join(output_dir, "mixed_audio.mp3")
        Path(mixed_audio).write_text("fake mixed audio")

        audio_path = os.path.join(output_dir, "tts_cjk.mp3")
        Path(audio_path).write_text("fake audio bytes")
        mock_tts.return_value = _make_tts_result(audio_path)

        mock_slideshow.return_value = os.path.join(output_dir, "slideshow_cjk.mp4")
        mock_compose.return_value = _make_composit_result(
            output_path=os.path.join(output_dir, "final_cjk.mp4"),
        )

        # ── Act ──────────────────────────────────────────────────────────
        result = await run_story2video_pipeline(
            article_text=text,
            output_dir=output_dir,
            max_scene_duration=2.0,
        )

        # ── Assert ───────────────────────────────────────────────────────
        assert result.success is True
        assert result.scenes > 0
        assert result.images_generated > 0
        assert result.images_generated == result.scenes
        assert result.output_path is not None
        mock_silent.assert_not_called()

    @patch("services.story2video.pipeline.text_to_speech", new_callable=AsyncMock)
    @patch("services.story2video.pipeline.mix_audio_clips")
    @patch("services.story2video.pipeline.create_slideshow")
    @patch("services.story2video.pipeline.compose_video")
    @patch("services.story2video.pipeline._generate_silent_audio")
    async def test_tts_per_scene(
        self,
        mock_silent: MagicMock,
        mock_compose: MagicMock,
        mock_slideshow: MagicMock,
        mock_mixer: MagicMock,
        mock_tts: AsyncMock,
        output_dir: str,
    ):
        """TTS is called once per scene with each scene's text."""
        # ── Arrange ──────────────────────────────────────────────────────
        text = "First scene text. Second scene text. Third scene text. Fourth scene text."

        mixed_audio = os.path.join(output_dir, "mixed_audio.mp3")
        Path(mixed_audio).write_text("fake mixed audio")

        audio_path = os.path.join(output_dir, "tts.mp3")
        Path(audio_path).write_text("fake audio bytes")
        mock_tts.return_value = _make_tts_result(audio_path)

        mock_slideshow.return_value = os.path.join(output_dir, "slideshow.mp4")
        mock_compose.return_value = _make_composit_result(
            output_path=os.path.join(output_dir, "final.mp4"),
        )

        # ── Act ──────────────────────────────────────────────────────────
        result = await run_story2video_pipeline(
            article_text=text,
            output_dir=output_dir,
            max_scene_duration=2.0,
        )

        # ── Assert ───────────────────────────────────────────────────────
        assert result.success is True
        assert mock_tts.call_count == result.scenes
        mock_silent.assert_not_called()

    @patch("services.story2video.pipeline.text_to_speech", new_callable=AsyncMock)
    @patch("services.story2video.pipeline.mix_audio_clips")
    @patch("services.story2video.pipeline.create_slideshow")
    @patch("services.story2video.pipeline.compose_video")
    @patch("services.story2video.pipeline._generate_silent_audio")
    async def test_tts_failure_falls_back_to_silence(
        self,
        mock_silent: MagicMock,
        mock_compose: MagicMock,
        mock_slideshow: MagicMock,
        mock_mixer: MagicMock,
        mock_tts: AsyncMock,
        output_dir: str,
    ):
        """When TTS fails for a scene, pipeline falls back to silent audio."""
        # ── Arrange ──────────────────────────────────────────────────────
        text = "A short sentence to fill one scene entirely. "
        text += "A second longer sentence that will create another scene."

        mixed_audio = os.path.join(output_dir, "mixed_audio.mp3")
        Path(mixed_audio).write_text("fake mixed audio")

        audio_path_ok = os.path.join(output_dir, "tts_ok.mp3")
        Path(audio_path_ok).write_text("fake audio bytes")

        mock_tts.side_effect = [
            _make_tts_result(audio_path_ok),                           # TTS succeeds
            _make_tts_result("", error="TTS failed: no key"),         # TTS fails
        ]
        mock_silent.return_value = True  # silent generation succeeds

        mock_slideshow.return_value = os.path.join(output_dir, "slideshow.mp4")
        mock_compose.return_value = _make_composit_result(
            output_path=os.path.join(output_dir, "final.mp4"),
        )

        # ── Act ──────────────────────────────────────────────────────────
        result = await run_story2video_pipeline(
            article_text=text,
            output_dir=output_dir,
            max_scene_duration=2.0,
        )

        # ── Assert ───────────────────────────────────────────────────────
        assert result.success is True
        assert result.scenes == 2
        mock_tts.assert_called()
        # Silent audio was generated for the failed scene
        mock_silent.assert_called_once()
        assert mock_mixer.called
        assert mock_slideshow.called
        assert mock_compose.called


# ── PipelineResult dataclass ────────────────────────────────────────────────


class TestPipelineResult:
    """Verify PipelineResult dataclass structure."""

    def test_pipeline_result_defaults(self):
        """PipelineResult has sensible defaults."""
        result = PipelineResult(success=True)
        assert result.success is True
        assert result.output_path is None
        assert result.scenes == 0
        assert result.images_generated == 0
        assert result.duration_seconds == 0.0
        assert result.error is None

    def test_pipeline_result_full(self):
        """PipelineResult with all fields populated."""
        result = PipelineResult(
            success=True,
            output_path="/tmp/final.mp4",
            scenes=3,
            images_generated=3,
            duration_seconds=12.0,
            error=None,
        )
        assert result.output_path == "/tmp/final.mp4"
        assert result.scenes == 3
        assert result.images_generated == 3
        assert result.duration_seconds == 12.0
