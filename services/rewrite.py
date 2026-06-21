"""LLM-based article rewriting service.

Extracted core logic from content-aggregator v2's rewriter.py.
Standalone — takes raw content string, no database dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from config import settings

# ── Style Prompts (from aggregator v2) ──────────────────────────────────────

STYLE_PROMPTS: dict[str, str] = {
    "轻松易懂": (
        "你是一位擅长将复杂内容转化为通俗易懂风格的内容改写专家。"
        "请将以下文章改写成轻松易懂的风格：用简洁明了的语言，避免专业术语，"
        "加入适当的比喻和例子，让普通人也能轻松理解。保持原文核心信息，"
        "但用更亲切、更接地气的表达方式。"
    ),
    "正式严谨": (
        "你是一位专业的商业内容编辑。"
        "请将以下文章改写成正式严谨的风格：使用规范的专业术语，"
        "逻辑严密，结构清晰，引用数据准确。适合正式场合或商业场景使用。"
    ),
    "吸引眼球": (
        "你是一位爆款内容创作者。"
        "请将以下文章改写成吸引眼球的风格：开头要有悬念或冲击力，"
        "中间层层递进，结尾有金句。善用排比、对比等修辞手法，"
        "让读者忍不住想看完并分享。"
    ),
    "深度分析": (
        "你是一位行业分析师。"
        "请将以下文章改写成深度分析的风格：从多角度剖析问题，"
        "提供数据支撑和逻辑推理，给出独到见解。适合专业读者深度阅读。"
    ),
}

LENGTH_INSTRUCTIONS: dict[str, str] = {
    "keep": "保持与原文相近的长度。",
    "compress": "压缩原文，保留核心信息，去除冗余内容，输出约为原文的50-70%长度。",
    "expand": "扩展原文，补充相关的背景知识、案例分析和数据说明，输出约为原文的150-200%长度。",
}


# ── Public API ──────────────────────────────────────────────────────────────


@dataclass
class RewriteResult:
    result_content: str
    word_count: int
    style: str
    length: str


def _count_words(text: str) -> int:
    """Count CJK characters + English words in mixed text."""
    import re

    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    english = len(re.findall(r"[a-zA-Z]+", text))
    return cjk + english


async def _call_llm(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_content: str,
    timeout: int = 120,
) -> str:
    """Call OpenAI-compatible LLM API."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.7,
                "max_tokens": 2000,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


async def rewrite_content(
    content: str,
    style: str = "轻松易懂",
    length: str = "keep",
    seo_optimize: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> RewriteResult:
    """Rewrite article content using LLM.

    Args:
        content: Raw article text to rewrite.
        style: One of 轻松易懂/正式严谨/吸引眼球/深度分析.
        length: One of keep/compress/expand.
        seo_optimize: Whether to add SEO hints.
        api_key: Override LLM API key (defaults to settings).
        base_url: Override LLM base URL.
        model: Override LLM model name.

    Returns:
        RewriteResult with rewritten content and word count.
    """
    if style not in STYLE_PROMPTS:
        raise ValueError(
            f"Unknown style '{style}'. Must be one of: {list(STYLE_PROMPTS.keys())}"
        )
    if length not in LENGTH_INSTRUCTIONS:
        raise ValueError(
            f"Unknown length '{length}'. Must be one of: {list(LENGTH_INSTRUCTIONS.keys())}"
        )

    system_prompt = STYLE_PROMPTS[style] + " " + LENGTH_INSTRUCTIONS[length]
    if seo_optimize:
        system_prompt += (
            " 注意：需要进行 SEO 优化，合理使用关键词，"
            "添加适当的标题层级和段落结构。"
        )

    # Use provided keys or fall back to settings/env
    key = api_key or settings.openai_api_key
    url = base_url or settings.openai_base_url
    mdl = model or settings.openai_model

    if not key:
        raise ValueError(
            "No LLM API key configured. Set PO_OPENAI_API_KEY env var "
            "or pass api_key parameter."
        )

    result = await _call_llm(
        api_key=key,
        base_url=url,
        model=mdl,
        system_prompt=system_prompt,
        user_content=content,
    )

    return RewriteResult(
        result_content=result,
        word_count=_count_words(result),
        style=style,
        length=length,
    )
