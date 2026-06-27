"""ImageGenBlock — 图片生成 Block。

封装 Image Service，为每个优化后的提示词生成场景图像。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from engine.block import Block, BlockOutput, register_block

logger = logging.getLogger(__name__)


class ImageGenInput(BaseModel):
    """图片生成 Block 输入。"""
    prompts: list[str] = Field(..., description="场景提示词列表")
    output_dir: str = Field("/tmp/pipeline/images", description="图片输出目录")


class ImageGenOutput(BaseModel):
    """图片生成 Block 输出。"""
    image_paths: list[str] = Field(..., description="生成的图片路径列表")


@register_block
class ImageGenBlock(Block[ImageGenInput, ImageGenOutput]):
    id = "image_gen"
    name = "图片生成"
    description = "为每个提示词生成对应场景的 AI 图像"
    version = "1.0.0"
    input_schema = ImageGenInput
    output_schema = ImageGenOutput

    async def run(self, inputs: ImageGenInput) -> AsyncGenerator[BlockOutput, None]:
        from services.image_service import GenerateImageRequest, generate_image

        os.makedirs(inputs.output_dir, exist_ok=True)
        paths: list[str] = []
        total = len(inputs.prompts)

        for i, prompt in enumerate(inputs.prompts):
            yield ("progress", json.dumps({
                "step": "imaging",
                "progress": (i + 1) / total,
                "scene_index": i,
            }))

            path = os.path.join(inputs.output_dir, f"scene_{i:03d}.png")
            req = GenerateImageRequest(prompt=prompt)
            result = await generate_image(req)

            if result.status == "success" or result.status == "completed":
                paths.append(result.image_url or path)
            else:
                logger.warning("Image gen failed for scene %d: %s", i, result.error)

        output = ImageGenOutput(image_paths=paths)
        yield ("image_paths", paths)
        yield ("output", output.model_dump())
