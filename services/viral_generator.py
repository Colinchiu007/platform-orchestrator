"""ViralCopyGenerator — 爆款文案生成引擎

Generates viral-optimized copy based on ViralFactorAnalyzer results.
LLM-powered with prompt templates for title generation, hook writing,
content rewriting, and structure suggestions.

Architecture:
  - Stateless generator, instantiated per request
  - Uses httpx to call OpenAI-compatible LLM APIs (same pattern as rewrite.py)
  - All prompts are built from templates in _PROMPTS dict
  - Output follows structured Pydantic models

Dependencies:
  - shared-models (ViralAnalysisResult, ArticleViralProfile, TitleStructure, etc.)
  - httpx for LLM calls
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from pydantic import BaseModel, Field

from config import settings
from shared_models.viral import (
    ArticleViralProfile,
    EmotionalTrigger,
    TitleStructure,
    ViralAnalysisResult,
    ViralScoringConfig,
)

logger = logging.getLogger(__name__)


# ── Prompt Templates ────────────────────────────────────────────────────
# Each template is a (system_prompt, user_prompt_template) pair.
# {placeholders} are filled at call time.

_PROMPTS: dict[str, tuple[str, str]] = {
    "generate_titles": (
        "你是一位资深爆款内容策划专家，擅长为自媒体创作者撰写高互动率的标题。\n\n"
        "## 核心能力\n"
        "- 熟悉小红书、抖音、公众号等平台的标题风格差异\n"
        "- 精通12种标题结构：疑问句、数字列表、How-to、对比式、悬念式、故事式、否定式、命令式、争议式、时效式、好奇缺口、直接式\n"
        "- 理解情感触发点：好奇、惊讶、争议、共情、焦虑、恐惧、喜悦、愤怒、励志\n\n"
        "## 输出要求\n"
        "- 每次生成{count}个标题\n"
        "- 每个标题附带：\n"
        "  - 标题文本\n"
        "  - 所属标题结构类型\n"
        "  - 触发的情感类型\n"
        "  - 预估互动力评分 (0-100)\n"
        "  - 简短的设计理由\n"
        "- 输出格式：JSON数组\n"
        "- 每个标题必须用不同的结构类型\n"
        "- 如果是特定平台，需要适配该平台的标题风格",
        "## 主题\n{topic}\n\n"
        "## 因子分析结果\n"
        "整体评分: {overall_score}\n"
        "趋势方向: {trend_direction}\n"
        "当前热门结构: {hot_structures}\n"
        "热门情感: {hot_emotions}\n"
        "平台: {platform}\n"
        "标题建议数量: {count}\n\n"
        "请生成{count}个爆款标题候选，覆盖不同的标题结构和情感触发类型。"
    ),
    "generate_hooks": (
        "你是一位爆款文案写作专家，擅长写文章开头Hook（钩子）。\n\n"
        "## 核心原则\n"
        "- Hook要在前3句话内抓住读者注意力\n"
        "- 可以使用：悬念、反常识、痛点共鸣、数据冲击、故事开场等手法\n"
        "- 不同平台的Hook风格不同：\n"
        "  - 小红书：亲切口语化，多用emoji，第一人称\n"
        "  - 抖音：前3秒抛出冲突/悬念/痛点\n"
        "  - 公众号：开场要有冲击力或深度共鸣\n\n"
        "## 输出要求\n"
        "- 生成{count}个不同的Hook\n"
        "- 每个Hook控制在100字以内\n"
        "- 附带简短说明（为什么这个Hook有效）",
        "## 文章标题\n{title}\n\n"
        "## 文章内容\n{content}\n\n"
        "## 目标平台\n{platform}\n\n"
        "请为这篇文章写{count}个不同的开头Hook。"
    ),
    "rewrite_content": (
        "你是一位专业的内容改写专家，擅长将文章适配到不同平台。\n\n"
        "## 改写原则\n"
        "1. 保持核心信息和观点不变\n"
        "2. 根据目标平台调整语气、长度和格式\n"
        "3. 加入平台特有的表达方式\n\n"
        "## 平台风格指南\n"
        "### 小红书\n"
        "- 语气：亲切、口语化、像朋友分享\n"
        "- 结构：开头Hook → 正文分点 → 总结推荐\n"
        "- 多用emoji、换行、分段\n"
        "- 标题控制在20字以内\n\n"
        "### 公众号\n"
        "- 语气：专业、深度、有观点\n"
        "- 结构：引言 → 分节论述 → 总结金句\n"
        "- 段落不宜过长，多用短句\n"
        "- 标题15-25字\n\n"
        "### 抖音\n"
        "- 语气：直接、有冲击力\n"
        "- 结构：前3秒Hook → 正文节奏紧凑 → 结尾引导互动\n"
        "- 短句为主，适合口播\n\n"
        "## 输出要求\n"
        "- 输出改写后的完整内容\n"
        "- 目标平台: {platform}\n"
        "- 风格: {style}",
        "## 原文内容\n{content}\n\n"
        "请将以上内容改写成适合{platform}平台的风格。"
    ),
    "suggest_structure": (
        "你是一位内容策略专家，擅长为文章设计最佳正文结构。\n\n"
        "## 正文结构类型\n"
        "1. **列表体（Listicle）**：分点列举，适合攻略、推荐类\n"
        "2. **故事体（Story）**：叙事手法，适合个人经历、品牌故事\n"
        "3. **教程体（Tutorial）**：步骤化教学，适合技能类\n"
        "4. **观点体（Opinion）**：个人观点输出，适合评论、分析\n"
        "5. **情绪体（Emotional）**：情感驱动，适合共鸣类内容\n"
        "6. **新闻体（News）**：信息密度高，适合资讯类\n"
        "7. **评测体（Review）**：多维度评价，适合产品对比\n"
        "8. **指南体（Guide）**：系统性介绍，适合入门教程\n\n"
        "## 输出要求\n"
        "- 推荐前3个最适合的结构，按匹配度排序\n"
        "- 每个结构附带评分(0-100)和建议理由\n"
        "- 给出详细的段落大纲",
        "## 主题\n{topic}\n\n"
        "## 分析结论\n"
        "热门标题结构: {hot_structures}\n"
        "热门情感: {hot_emotions}\n"
        "目标平台: {platform}\n\n"
        "请为这个主题推荐最佳的正文结构，并给出详细大纲。"
    ),
}


# ── Generation Result Models ────────────────────────────────────────────


class TitleSuggestion(BaseModel):
    """单条标题建议"""
    title: str = ""
    structure: str = ""
    emotion: str = ""
    predicted_score: float = 0.0
    reasoning: str = ""


class TitleGenerationResult(BaseModel):
    """标题生成结果"""
    titles: list[TitleSuggestion] = Field(default_factory=list)
    topic: str = ""
    platform: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class HookSuggestion(BaseModel):
    """单条Hook建议"""
    hook: str = ""
    technique: str = ""
    reasoning: str = ""


class HookGenerationResult(BaseModel):
    """Hook生成结果"""
    hooks: list[HookSuggestion] = Field(default_factory=list)
    title: str = ""
    platform: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class StructureSuggestion(BaseModel):
    """单条结构建议"""
    structure: str = ""
    structure_name: str = ""
    score: float = 0.0
    reasoning: str = ""
    outline: str = ""


class StructureSuggestionResult(BaseModel):
    """结构建议结果"""
    suggestions: list[StructureSuggestion] = Field(default_factory=list)
    topic: str = ""
    platform: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class RewriteResult(BaseModel):
    """内容改写结果"""
    rewritten_content: str = ""
    platform: str = ""
    style: str = ""
    original_word_count: int = 0
    rewritten_word_count: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


# ── Generator ───────────────────────────────────────────────────────────


def _count_words(text: str) -> int:
    """Count CJK characters + English words in mixed text."""
    cjk = len(re.findall(r"[一-鿿㐀-䶿]", text))
    english = len(re.findall(r"[a-zA-Z]+", text))
    return cjk + english


async def _call_llm(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float = 0.8,
    max_tokens: int = 2000,
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
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


def _parse_json_array(text: str) -> list[dict]:
    """Try to extract a JSON array from LLM output, with fallback parsing."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("```"):
        # Remove markdown code fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Try to find JSON array in the text
    array_match = re.search(r"\[.*\]", text, re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group())
        except json.JSONDecodeError:
            pass

    # Try parsing the whole text as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: try to find individual objects and build array
    objects = []
    obj_pattern = re.compile(r"\{[^}]+\}")
    for match in obj_pattern.finditer(text):
        try:
            obj = json.loads(match.group())
            objects.append(obj)
        except json.JSONDecodeError:
            pass

    return objects


class ViralCopyGenerator:
    """爆款文案生成引擎

    Generates viral-optimized copy using LLM with structured prompt templates.
    Stateless — all generation state is passed as arguments.

    Usage:
        generator = ViralCopyGenerator()
        titles = await generator.generate_titles(
            topic="AI工具推荐",
            analysis=analysis_result,
            platform="xiaohongshu"
        )
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        scoring_config: Optional[ViralScoringConfig] = None,
    ):
        self._api_key = api_key or settings.openai_api_key
        self._base_url = base_url or settings.openai_base_url
        self._model = model or settings.openai_model
        self._scoring_config = scoring_config or ViralScoringConfig()

    # ── Public API ──────────────────────────────────────────────────────

    async def generate_titles(
        self,
        topic: str,
        analysis: Optional[ViralAnalysisResult] = None,
        platform: str = "通用",
        count: int = 5,
        temperature: float = 0.8,
    ) -> TitleGenerationResult:
        """生成爆款标题候选列表。

        Args:
            topic: 主题关键词
            analysis: 可选的因子分析结果，用于指导标题生成
            platform: 目标平台
            count: 生成标题数量
            temperature: LLM 温度参数

        Returns:
            TitleGenerationResult with title suggestions
        """
        # Extract insights from analysis if provided
        hot_structures = ""
        hot_emotions = ""
        overall_score = "N/A"
        trend_direction = "stable"

        if analysis:
            overall_score = f"{analysis.overall_score:.1f}"
            trend_direction = analysis.trend_direction
            if analysis.suggested_structures:
                hot_structures = ", ".join(
                    s.get("structure", "") for s in analysis.suggested_structures
                )
            if analysis.factors:
                emotion_factors = [
                    f for f in analysis.factors
                    if f.name in ("emotion", "emotional_trigger")
                ]
                if emotion_factors:
                    hot_emotions = emotion_factors[0].label

        sys_prompt, user_template = _PROMPTS["generate_titles"]
        user_content = user_template.format(
            topic=topic,
            overall_score=overall_score,
            trend_direction=trend_direction,
            hot_structures=hot_structures or "暂无数据",
            hot_emotions=hot_emotions or "暂无数据",
            platform=platform,
            count=count,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
        )

        suggestions = self._parse_title_suggestions(raw)
        return TitleGenerationResult(
            titles=suggestions[:count],
            topic=topic,
            platform=platform,
        )

    async def generate_hooks(
        self,
        title: str,
        content: str = "",
        platform: str = "通用",
        count: int = 3,
        temperature: float = 0.8,
    ) -> HookGenerationResult:
        """生成文章开头Hook（钩子）建议。

        Args:
            title: 文章标题
            content: 文章正文内容（可选）
            platform: 目标平台
            count: 生成Hook数量
            temperature: LLM 温度参数

        Returns:
            HookGenerationResult with hook suggestions
        """
        sys_prompt, user_template = _PROMPTS["generate_hooks"]
        user_content = user_template.format(
            title=title,
            content=content or "（暂无正文内容，请基于标题生成通用Hook）",
            platform=platform,
            count=count,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
        )

        suggestions = self._parse_hook_suggestions(raw)
        return HookGenerationResult(
            hooks=suggestions[:count],
            title=title,
            platform=platform,
        )

    async def rewrite_content(
        self,
        content: str,
        platform: str = "通用",
        style: str = "自动适配",
        temperature: float = 0.7,
    ) -> RewriteResult:
        """将内容改写为目标平台风格。

        Args:
            content: 原文内容
            platform: 目标平台
            style: 写作风格
            temperature: LLM 温度参数

        Returns:
            RewriteResult with rewritten content
        """
        original_wc = _count_words(content)

        sys_prompt, user_template = _PROMPTS["rewrite_content"]
        sys_prompt = sys_prompt.replace("{platform}", platform)
        sys_prompt = sys_prompt.replace("{style}", style)

        user_content = user_template.format(content=content, platform=platform)

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=4096,
        )

        return RewriteResult(
            rewritten_content=raw.strip(),
            platform=platform,
            style=style,
            original_word_count=original_wc,
            rewritten_word_count=_count_words(raw),
        )

    async def suggest_structures(
        self,
        topic: str,
        analysis: Optional[ViralAnalysisResult] = None,
        platform: str = "通用",
        temperature: float = 0.7,
    ) -> StructureSuggestionResult:
        """推荐正文结构并给出大纲。

        Args:
            topic: 主题关键词
            analysis: 可选的因子分析结果
            platform: 目标平台
            temperature: LLM 温度参数

        Returns:
            StructureSuggestionResult with structure suggestions
        """
        hot_structures = ""
        hot_emotions = ""

        if analysis:
            if analysis.suggested_structures:
                hot_structures = ", ".join(
                    s.get("structure", "") for s in analysis.suggested_structures
                )
            if analysis.factors:
                emotion_factors = [
                    f for f in analysis.factors
                    if f.name in ("emotion", "emotional_trigger")
                ]
                if emotion_factors:
                    hot_emotions = emotion_factors[0].label

        sys_prompt, user_template = _PROMPTS["suggest_structure"]
        user_content = user_template.format(
            topic=topic,
            hot_structures=hot_structures or "暂无数据",
            hot_emotions=hot_emotions or "暂无数据",
            platform=platform,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
        )

        suggestions = self._parse_structure_suggestions(raw)
        return StructureSuggestionResult(
            suggestions=suggestions[:3],
            topic=topic,
            platform=platform,
        )

    # ── Internal ────────────────────────────────────────────────────────

    async def _call(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.8,
        max_tokens: int = 2000,
    ) -> str:
        """Execute LLM call with error handling."""
        if not self._api_key:
            raise ValueError(
                "No LLM API key configured. Set PO_OPENAI_API_KEY env var "
                "or pass api_key to ViralCopyGenerator."
            )
        try:
            return await _call_llm(
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM API error: {e.response.status_code} {e.response.text}")
            raise
        except httpx.TimeoutException:
            logger.error("LLM API timeout")
            raise
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

    def _parse_title_suggestions(self, raw: str) -> list[TitleSuggestion]:
        """Parse LLM output into TitleSuggestion list."""
        parsed = _parse_json_array(raw)
        suggestions: list[TitleSuggestion] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = item.get("title", item.get("text", ""))
            if not title:
                continue
            suggestions.append(TitleSuggestion(
                title=title,
                structure=item.get("structure", item.get("type", "")),
                emotion=item.get("emotion", ""),
                predicted_score=float(item.get("predicted_score", item.get("score", 0))),
                reasoning=item.get("reasoning", item.get("reason", "")),
            ))

        # Fallback: line-by-line parsing if JSON parsing yielded nothing
        if not suggestions:
            for line in raw.strip().split("\n"):
                line = line.strip().strip("-*").strip()
                if line and len(line) > 5 and not line.startswith("{"):
                    suggestions.append(TitleSuggestion(title=line[:100]))

        return suggestions

    def _parse_hook_suggestions(self, raw: str) -> list[HookSuggestion]:
        """Parse LLM output into HookSuggestion list."""
        parsed = _parse_json_array(raw)
        suggestions: list[HookSuggestion] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            hook = item.get("hook", item.get("text", ""))
            if not hook:
                continue
            suggestions.append(HookSuggestion(
                hook=hook,
                technique=item.get("technique", item.get("type", "")),
                reasoning=item.get("reasoning", item.get("reason", "")),
            ))

        if not suggestions:
            for line in raw.strip().split("\n"):
                line = line.strip().strip("-*").strip()
                if line and len(line) > 10:
                    suggestions.append(HookSuggestion(hook=line[:200]))

        return suggestions

    def _parse_structure_suggestions(self, raw: str) -> list[StructureSuggestion]:
        """Parse LLM output into StructureSuggestion list."""
        parsed = _parse_json_array(raw)
        suggestions: list[StructureSuggestion] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            structure = item.get("structure", item.get("type", ""))
            if not structure:
                continue
            suggestions.append(StructureSuggestion(
                structure=structure,
                structure_name=item.get("structure_name", item.get("name", "")),
                score=float(item.get("score", item.get("predicted_score", 0))),
                reasoning=item.get("reasoning", item.get("reason", "")),
                outline=item.get("outline", ""),
            ))

        if not suggestions:
            for line in raw.strip().split("\n"):
                line = line.strip().strip("-*").strip()
                if line and len(line) > 5:
                    suggestions.append(StructureSuggestion(structure=line[:100]))

        return suggestions


# ── Factory ─────────────────────────────────────────────────────────────


def create_generator(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    scoring_config: Optional[ViralScoringConfig] = None,
) -> ViralCopyGenerator:
    """Create a ViralCopyGenerator with optional overrides.

    Args:
        api_key: Override LLM API key (defaults to settings.openai_api_key)
        base_url: Override LLM base URL (defaults to settings.openai_base_url)
        model: Override LLM model (defaults to settings.openai_model)
        scoring_config: Optional ViralScoringConfig for score computations

    Returns:
        Configured ViralCopyGenerator instance
    """
    return ViralCopyGenerator(
        api_key=api_key,
        base_url=base_url,
        model=model,
        scoring_config=scoring_config,
    )
