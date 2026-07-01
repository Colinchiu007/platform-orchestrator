"""Blocks 包 — 所有示例 Block 实现集中注册。

导入此模块即触发所有 Block 自动注册到全局 _BLOCK_REGISTRY。
"""

from engine import register_block

from blocks.splitter_block import SplitterBlock
from blocks.optimizer_block import OptimizerBlock
from blocks.tts_block import TTSBlock
from blocks.image_gen_block import ImageGenBlock
from blocks.compose_block import ComposeBlock

# 导入即注册（@register_block 装饰器在类定义时已执行）
# 这里显式导入确保 blocks 包被引用时所有 Block 都加载

__all__ = [
    "SplitterBlock",
    "OptimizerBlock",
    "TTSBlock",
    "ImageGenBlock",
    "ComposeBlock",
]
