"""Prompt optimization service — LLM-based scene-to-prompt optimization.

Replaces the optimize-prompt Edge Function.
Reuses the same _call_llm pattern from services/rewrite.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx

from config import settings
from services.rewrite import _call_llm

# ── Default System Prompt ───────────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = """你是一位专业的AI图像生成提示词专家。
请将以下场景描述优化为高质量的图像生成提示词。

要求：
1. 描述视觉元素：主体、环境、光线、色彩、构图
2. 指定艺术风格和氛围
3. 添加必要的技术参数（比例、画质等）
4. 保持与原文的语义一致性
5. 输出简洁、精准的英文或中文提示词

直接输出优化后的提示词，不要包含解释性文字。"""


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class OptimizePromptResult:
    prompts: List[str]
    error: Optional[str] = None


# ── Public API ──────────────────────────────────────────────────────────────


async def optimize_prompt(
    text: str,
    segments: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> OptimizePromptResult:
    """Optimize scene text into image generation prompts.

    Args:
        text: Full scene text or primary prompt input.
        segments: Optional list of sub-segments to optimize individually.
        system_prompt: Custom system prompt (defaults to built-in).
        api_key: LLM API key override.
        base_url: LLM base URL override.
        model: LLM model override.

    Returns:
        OptimizePromptResult with list of optimized prompts.
    """
    key = api_key or settings.openai_api_key
    url = base_url or settings.openai_base_url
    mdl = model or settings.openai_model

    if not key:
        return OptimizePromptResult(prompts=[], error="No LLM API key configured")

    sys_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    prompts: List[str] = []
    inputs = segments if segments else [text]

    for segment in inputs:
        if not segment.strip():
            continue

        try:
            result = await _call_llm(
                api_key=key,
                base_url=url,
                model=mdl,
                system_prompt=sys_prompt,
                user_content=segment,
            )
            prompts.append(result.strip())
        except Exception as e:
            prompts.append(f"[ERROR] {str(e)}")

    return OptimizePromptResult(prompts=prompts)


async def optimize_prompts_batch(
    scenes: List[dict],
    api_key: Optional[str] = None,
) -> OptimizePromptResult:
    """Optimize prompts for multiple scenes at once.

    Each scene dict should have 'text' key (the scene text to optimize).

    Args:
        scenes: List of scene dicts with 'text' field.
        api_key: LLM API key.

    Returns:
        OptimizePromptResult with one prompt per scene.
    """
    texts = [s.get("text", "") for s in scenes]
    return await optimize_prompt(text="", segments=texts, api_key=api_key)
