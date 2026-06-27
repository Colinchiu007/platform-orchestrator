"""OptimizerBlock — 提示词优化 Block。

封装 Prompt-Engine，为每个场景生成 AI 图像提示词。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from engine.block import Block, BlockOutput, register_block


class OptimizerInput(BaseModel):
    """优化 Block 输入。"""
    scenes: list[dict[str, Any]] = Field(..., description="分句结果中的场景列表")
    platform: str = Field("midjourney", description="目标图像平台")


class OptimizerOutput(BaseModel):
    """优化 Block 输出。"""
    prompts: list[str] = Field(..., description="优化后的提示词列表（每场景一个）")
    total_prompts: int = Field(..., description="提示词总数")


@register_block
class OptimizerBlock(Block[OptimizerInput, OptimizerOutput]):
    id = "optimizer"
    name = "提示词优化"
    description = "为每个场景生成优化后的 AI 图像生成提示词"
    version = "1.0.0"
    input_schema = OptimizerInput
    output_schema = OptimizerOutput

    async def run(self, inputs: OptimizerInput) -> AsyncGenerator[BlockOutput, None]:
        from prompt_engine import Optimizer, OptimizeRequest, PlatformType

        platform_map = {
            "midjourney": PlatformType.MIDJOURNEY,
            "stable_diffusion": PlatformType.STABLE_DIFFUSION,
            "dall_e": PlatformType.DALLE,
            "sd_xl": PlatformType.STABLE_DIFFUSION,
            "flux": PlatformType.GENERIC,
            "kling": PlatformType.GENERIC,
            "cogview": PlatformType.GENERIC,
        }
        pt = platform_map.get(inputs.platform, PlatformType.GENERIC)
        optimizer = Optimizer()

        prompts: list[str] = []
        total = len(inputs.scenes)

        for i, scene in enumerate(inputs.scenes):
            yield ("progress", json.dumps({
                "step": "optimizing",
                "progress": (i + 1) / total,
                "scene_index": i,
            }))

            req = OptimizeRequest(
                prompt=scene.get("text", "")[:1500],
                platform=pt,
                creative_level=5,
                max_length=300,
                num_candidates=1,
            )
            result = await asyncio.to_thread(optimizer.optimize, req)
            prompts.append(result.optimized_prompt or scene.get("text", ""))

        output = OptimizerOutput(prompts=prompts, total_prompts=len(prompts))
        yield ("prompts", prompts)
        yield ("output", output.model_dump())
