"""SplitterBlock — 智能分句 Block。

封装 Smart-Sentence-Splitter，将长文本拆分为场景块。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from engine.block import Block, BlockOutput, register_block


class SplitterInput(BaseModel):
    """分句 Block 输入。"""
    content: str = Field(..., description="待拆分的原始文本")
    language: str = Field("zh", description="文本语言")


class SplitterOutput(BaseModel):
    """分句 Block 输出。"""
    scenes: list[dict[str, Any]] = Field(..., description="拆分后的场景列表")
    total_scenes: int = Field(..., description="场景总数")


@register_block
class SplitterBlock(Block[SplitterInput, SplitterOutput]):
    id = "splitter"
    name = "智能分句"
    description = "将长文本拆分为场景块，适配语音和画面合成"
    version = "1.0.0"
    input_schema = SplitterInput
    output_schema = SplitterOutput

    async def run(self, inputs: SplitterInput) -> AsyncGenerator[BlockOutput, None]:
        yield ("progress", json.dumps({"step": "splitting", "progress": 0.1}))

        from splitter import SmartSentenceSplitter

        splitter = SmartSentenceSplitter({"language": inputs.language})
        result = await asyncio.to_thread(splitter.split, inputs.content)

        scenes = [
            {
                "text": s.text,
                "segment_id": s.segment_id,
                "estimated_duration": s.estimated_duration,
                "sentences": [st.text for st in s.sentences],
            }
            for s in result.scenes
        ]

        output = SplitterOutput(scenes=scenes, total_scenes=len(scenes))
        yield ("progress", json.dumps({"step": "splitting", "progress": 1.0}))
        yield ("scenes", scenes)
        yield ("output", output.model_dump())
