"""ComposeBlock — 视频合成 Block。

封装 video-compositor (FFmpeg)，将图片 + 音频合成为最终 MP4 视频。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from engine.block import Block, BlockOutput, register_block


class ComposeInput(BaseModel):
    """合成 Block 输入。"""
    image_paths: list[str] = Field(..., description="图片路径列表")
    audio_path: str = Field(..., description="音频文件路径")
    output_path: str = Field("", description="输出视频路径（可选）")
    width: int = Field(1280, description="视频宽度")
    height: int = Field(720, description="视频高度")
    image_duration: float = Field(6.0, description="每张图片展示秒数")
    fps: int = Field(30, description="帧率")
    prompt_platform: str = Field("midjourney", description="关联的提示词平台")


class ComposeOutput(BaseModel):
    """合成 Block 输出。"""
    output_path: str = Field(..., description="最终视频文件路径")
    duration_seconds: float = Field(0.0, description="视频时长(秒)")


@register_block
class ComposeBlock(Block[ComposeInput, ComposeOutput]):
    id = "compose"
    name = "视频合成"
    description = "将图片和音频合成为最终 MP4 视频"
    version = "1.0.0"
    input_schema = ComposeInput
    output_schema = ComposeOutput

    async def run(self, inputs: ComposeInput) -> AsyncGenerator[BlockOutput, None]:
        from video_compositor import CompositorInput, compose_video

        yield ("progress", json.dumps({"step": "composing", "progress": 0.1}))

        inp = CompositorInput(
            images=inputs.image_paths,
            audio_path=inputs.audio_path,
            output_path=inputs.output_path or None,
            width=inputs.width,
            height=inputs.height,
            image_duration=inputs.image_duration,
            fps=inputs.fps,
        )
        result = await asyncio.to_thread(compose_video, inp)

        if not result.success:
            raise RuntimeError(f"视频合成失败: {result.error}")

        yield ("progress", json.dumps({"step": "composing", "progress": 1.0}))
        yield ("output_path", result.output_path)

        output = ComposeOutput(
            output_path=result.output_path,
            duration_seconds=result.duration_seconds,
        )
        yield ("output", output.model_dump())
