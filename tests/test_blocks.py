"""Block 实现单元测试 - 5 个 Block 的输入/输出契约验证。

测试策略：
- 使用 mock 替代外部服务（LLM、TTS、图片生成、FFmpeg 合成）
- 验证每个 Block 的输入模型解析、输出格式、progress 流式输出
- 不启动 FastAPI，不连接真实服务
- 遵循 test_engine.py 的测试模式：clear_registry -> 导入块 -> 收集 yields
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

import sys
from unittest.mock import MagicMock, patch

import pytest

from engine.block import Block, clear_registry


# -- Helpers ----------------------------------------------------------------------


async def _collect_yields(block: Block, inputs) -> dict:
    """Run a Block and collect all yielded (key, value) pairs into a dict."""
    collected = {}
    async for key, value in block.run(inputs):
        collected[key] = value
    return collected


# -- Fixtures ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_registry():
    clear_registry()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ==================== SplitterBlock ==============================================


class TestSplitterBlock:
    """SplitterBlock: 封装 Smart-Sentence-Splitter。"""

    def _import_block(self):
        from blocks.splitter_block import SplitterBlock
        return SplitterBlock

    async def _run(self, content: str = "Hello world.", language: str = "en"):
        cls = self._import_block()
        from blocks.splitter_block import SplitterInput
        return await _collect_yields(cls(), SplitterInput(content=content, language=language))

    @patch("splitter.SmartSentenceSplitter")
    async def test_basic_split(self, MockSplitter):
        inst = MockSplitter.return_value
        inst.split.return_value = MagicMock(
            scenes=[
                MagicMock(text="First scene.", segment_id=0, estimated_duration=5.0,
                          sentences=[MagicMock(text="First sentence.")]),
                MagicMock(text="Second scene.", segment_id=1, estimated_duration=3.0,
                          sentences=[MagicMock(text="Second sentence.")]),
            ],
        )
        result = await self._run()
        assert "progress" in result
        assert len(result["scenes"]) == 2
        assert result["scenes"][0]["text"] == "First scene."
        assert result["output"]["total_scenes"] == 2

    @patch("splitter.SmartSentenceSplitter")
    async def test_empty_content(self, MockSplitter):
        MockSplitter.return_value.split.return_value = MagicMock(scenes=[])
        result = await self._run(content="", language="zh")
        assert result["output"]["total_scenes"] == 0
        assert result["scenes"] == []

    def test_input_schema_validates(self):
        from blocks.splitter_block import SplitterInput
        inp = SplitterInput(content="test content")
        assert inp.content == "test content"
        assert inp.language == "zh"  # default

    def test_block_metadata(self):
        cls = self._import_block()
        assert cls.id == "splitter"
        assert cls.version == "1.0.0"


# ==================== OptimizerBlock =============================================


class TestOptimizerBlock:
    """OptimizerBlock: 封装 Prompt-Engine。"""

    def _import_block(self):
        from blocks.optimizer_block import OptimizerBlock
        return OptimizerBlock

    async def _run(self, scenes: list[dict] | None = None, platform: str = "midjourney"):
        cls = self._import_block()
        from blocks.optimizer_block import OptimizerInput
        return await _collect_yields(
            cls(),
            OptimizerInput(scenes=scenes or [{"text": "A cat on a mat."}], platform=platform),
        )

    @patch("prompt_engine.Optimizer")
    async def test_basic_optimize(self, MockOptimizer):
        MockOptimizer.return_value.optimize.return_value = MagicMock(
            optimized_prompt="A beautiful cat sitting on a woven mat, cinematic lighting.",
        )
        result = await self._run()
        assert len(result["prompts"]) == 1
        assert "cat" in result["prompts"][0].lower()
        assert result["output"]["total_prompts"] == 1

    @patch("prompt_engine.Optimizer")
    async def test_multiple_scenes(self, MockOptimizer):
        MockOptimizer.return_value.optimize.return_value = MagicMock(optimized_prompt="A prompt.")
        result = await self._run(scenes=[{"text": "S1"}, {"text": "S2"}, {"text": "S3"}])
        assert len(result["prompts"]) == 3
        assert result["output"]["total_prompts"] == 3

    @patch("prompt_engine.Optimizer")
    async def test_platform_mapping(self, MockOptimizer):
        MockOptimizer.return_value.optimize.return_value = MagicMock(optimized_prompt="Prompt.")
        result = await self._run(platform="stable_diffusion")
        assert "prompts" in result

    def test_block_metadata(self):
        assert self._import_block().id == "optimizer"


# ==================== TTSBlock ===================================================


class TestTTSBlock:
    """TTSBlock: 封装 TTS Service。"""

    def _import_block(self):
        from blocks.tts_block import TTSBlock
        return TTSBlock

    async def _run(self, scenes: list[dict] | None = None, voice: str = "zh-CN-XiaoxiaoNeural",
                   output_dir: str = "/tmp/test_tts"):
        cls = self._import_block()
        from blocks.tts_block import TTSInput
        return await _collect_yields(
            cls(),
            TTSInput(scenes=scenes or [{"text": "Hello world."}], voice=voice, output_dir=output_dir),
        )

    @patch("services.tts_service.text_to_speech")
    async def test_basic_tts(self, mock_tts):
        mock_tts.return_value = MagicMock(audio_path="/tmp/tts/audio.mp3", duration_seconds=5.0)
        result = await self._run()
        assert result["audio_path"] == "/tmp/tts/audio.mp3"
        assert result["output"]["duration_seconds"] == 5.0

    @patch("services.tts_service.text_to_speech")
    async def test_output_dir_created(self, mock_tts, tmp_dir):
        out = os.path.join(tmp_dir, "tts_output")
        mock_tts.return_value = MagicMock(audio_path=f"{out}/audio.mp3", duration_seconds=3.0)
        await self._run(output_dir=out)
        assert os.path.isdir(out)

    def test_block_metadata(self):
        assert self._import_block().id == "tts"


# ==================== ImageGenBlock ==============================================


class TestImageGenBlock:
    """ImageGenBlock: 封装 Image Service。"""

    def _import_block(self):
        from blocks.image_gen_block import ImageGenBlock
        return ImageGenBlock

    async def _run(self, prompts: list[str] | None = None, output_dir: str = "/tmp/test_img"):
        cls = self._import_block()
        from blocks.image_gen_block import ImageGenInput
        return await _collect_yields(
            cls(),
            ImageGenInput(prompts=prompts or ["A cat."], output_dir=output_dir),
        )

    @patch("services.image_service.generate_image")
    async def test_basic_generation(self, mock_gen):
        mock_gen.return_value = MagicMock(status="success", image_url="/img/scene_000.png")
        result = await self._run()
        assert len(result["image_paths"]) == 1

    @patch("services.image_service.generate_image")
    async def test_multiple_prompts(self, mock_gen):
        mock_gen.return_value = MagicMock(status="success", image_url="/img.png")
        result = await self._run(prompts=["A", "B", "C"])
        assert len(result["image_paths"]) == 3

    @patch("services.image_service.generate_image")
    async def test_failed_generation_skipped(self, mock_gen):
        mock_gen.return_value = MagicMock(status="failed", error="API error", image_url="")
        result = await self._run(prompts=["A cat."])
        assert result["image_paths"] == []

    def test_block_metadata(self):
        assert self._import_block().id == "image_gen"


# ==================== ComposeBlock ===============================================


class _MockCompositorInput:
    """Real class so CompositorInput(width=1920) stores attrs."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

@pytest.fixture
def mock_video_compositor():
    """Inject mock video_compositor module so ComposeBlock can lazy-import it."""
    import types
    mock_mod = types.ModuleType("video_compositor")
    mock_mod.compose_video = MagicMock()
    mock_mod.CompositorInput = _MockCompositorInput
    with patch.dict("sys.modules", {"video_compositor": mock_mod}):
        yield mock_mod


class TestComposeBlock:
    """ComposeBlock: 封装 video-compositor (FFmpeg)。"""

    def _import_block(self):
        from blocks.compose_block import ComposeBlock
        return ComposeBlock

    async def _run(self, **kwargs):
        cls = self._import_block()
        from blocks.compose_block import ComposeInput
        defaults = {"image_paths": ["/i1.png"], "audio_path": "/a.mp3"}
        defaults.update(kwargs)
        return await _collect_yields(cls(), ComposeInput(**defaults))

    async def test_basic_compose(self, mock_video_compositor):
        mock_video_compositor.compose_video.return_value = MagicMock(
            success=True, output_path="/out.mp4", duration_seconds=12.0,
        )
        result = await self._run()
        assert result["output_path"] == "/out.mp4"
        assert result["output"]["duration_seconds"] == 12.0

    async def test_compose_failure_raises(self, mock_video_compositor):
        mock_video_compositor.compose_video.return_value = MagicMock(success=False, error="FFmpeg error")
        with pytest.raises(RuntimeError, match="视频合成失败"):
            await self._run()

    async def test_custom_dimensions(self, mock_video_compositor):
        mock_video_compositor.compose_video.return_value = MagicMock(success=True, output_path="/out.mp4", duration_seconds=10.0)
        await self._run(width=1920, height=1080, fps=24)
        inp = mock_video_compositor.compose_video.call_args[0][0]
        assert inp.width == 1920
        assert inp.height == 1080
        assert inp.fps == 24

    def test_block_metadata(self):
        assert self._import_block().id == "compose"
