"""TTSBlock — 语音合成 Block。

封装 TTS service（豆包/火山引擎 TTS），将场景文本合成为音频。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from engine.block import Block, BlockOutput, register_block


class TTSInput(BaseModel):
    """TTS Block 输入。"""
    scenes: list[dict[str, Any]] = Field(..., description="含 text 字段的场景列表")
    voice: str = Field("zh-CN-XiaoxiaoNeural", description="音色 ID")
    output_dir: str = Field("/tmp/pipeline/tts", description="音频输出目录")


class TTSOutput(BaseModel):
    """TTS Block 输出。"""
    audio_path: str = Field(..., description="合成音频文件路径")
    duration_seconds: float = Field(0.0, description="音频时长(秒)")


@register_block
class TTSBlock(Block[TTSInput, TTSOutput]):
    id = "tts"
    name = "语音合成"
    description = "将场景文本合成为语音音频"
    version = "1.0.0"
    input_schema = TTSInput
    output_schema = TTSOutput

    async def run(self, inputs: TTSInput) -> AsyncGenerator[BlockOutput, None]:
        from services.tts_service import text_to_speech

        yield ("progress", json.dumps({"step": "tts", "progress": 0.1}))

        full_text = " ".join(s.get("text", "") for s in inputs.scenes)
        os.makedirs(inputs.output_dir, exist_ok=True)
        output_path = os.path.join(inputs.output_dir, "audio.mp3")

        result = await text_to_speech(full_text, voice_name=inputs.voice, output_path=output_path)

        yield ("progress", json.dumps({"step": "tts", "progress": 1.0}))
        yield ("audio_path", result.audio_path)
        yield (
            "output",
            TTSOutput(
                audio_path=result.audio_path,
                duration_seconds=result.duration_seconds,
            ).model_dump(),
        )
