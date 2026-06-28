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
    # ── SEO/CRO 扩展 ─────────────────────────────────────────────────────
    "generate_seo_titles": (
        "你是一位 SEO 内容策略专家，擅长为搜索流量生成高点击率标题。\n\n"
        "## 核心能力\n"
        "- 理解搜索意图分类：信息型(info)、商业型(commercial)、交易型(transactional)、导航型(navigational)\n"
        "- 精通 SEO 标题优化：关键词前置、字符控制(50-60字符)、品牌词位置\n"
        "- 融合爆款元素与搜索引擎友好度\n"
        "- 遵循 Google E-E-A-T 标准（经验/专业/权威/信任）\n\n"
        "## 标题结构类型\n"
        "1. 数字列表式：'N个[关键词]方法/技巧/趋势'\n"
        "2. How-to 式：'如何[达成目标]（N步指南）'\n"
        "3. 对比式：'[A] vs [B]：哪个更适合[场景]'\n"
        "4. 问题式：'为什么[常见问题]？[解决方案]'\n"
        "5. 最佳式：'N个最佳[分类][年份]'\n"
        "6. 指南式：'[主题]完整指南：从入门到精通'\n"
        "7. 数据式：'[数字]%的[人群]正在[趋势]（[数据年份]）'\n"
        "8. 地域式：'[城市][服务]推荐/N选'\n\n"
        "## 输出要求\n"
        "- 每次生成{count}个标题\n"
        "- 每个标题附带：标题文本、标题结构类型、搜索意图、字符数、预估CTR(0-100)、设计理由\n"
        "- 标题长度控制在50-60字符\n"
        "- 主关键词尽量前置\n"
        "- 输出格式：JSON数组",
        "## 主题\n{topic}\n\n"
        "## 主要关键词\n{keywords}\n\n"
        "## 搜索意图\n{search_intent}\n\n"
        "## 目标平台\n{platform}\n\n"
        "## 额外上下文\n{context}\n\n"
        "请生成{count}个 SEO 优化的标题候选，覆盖不同结构和搜索意图。"
    ),
    "analyze_cro": (
        "你是一位转化率优化(CRO)专家，擅长分析营销页面并提供可操作的改进建议。\n\n"
        "## CRO 分析框架（按影响力排序）\n\n"
        "### 1. 价值主张清晰度（最高影响）\n"
        "- 访客5秒内能否理解这是什么、为什么值得关注？\n"
        "- 主要收益是否清晰、具体、有差异化？\n"
        "- 是否使用客户语言（而非公司术语）？\n\n"
        "### 2. 标题有效性\n"
        "- 标题是否传达核心价值主张？\n"
        "- 是否匹配搜索来源的预期？\n"
        "- 是否足够具体（数字/时间/成果）？\n\n"
        "### 3. CTA 层级与优化\n"
        "- 是否有一个清晰的主要行动？\n"
        "- 不滚动能否看到 CTA？\n"
        "- 按钮文案是否传达价值（而非仅动作）？\n"
        "  - 弱：'提交'/'注册'/'了解更多'\n"
        "  - 强：'免费试用'/'获取报告'/'查看定价'\n\n"
        "### 4. 视觉层级与可扫描性\n"
        "- 扫码用户能否抓住核心信息？\n"
        "- 最重要元素是否视觉突出？\n"
        "- 白空间是否充足？\n\n"
        "### 5. 信任信号\n"
        "- 客户logo（尤其知名品牌）\n"
        "- 推荐语（具体、署名、带照片）\n"
        "- 案例数据（真实数字）\n"
        "- 评分/评论数\n"
        "- 安全标识\n\n"
        "### 6. 异议处理\n"
        "- FAQ 解答常见疑虑\n"
        "- 保证/退款政策\n"
        "- 价格透明度\n\n"
        "### 7. 摩擦点\n"
        "- 表单项过多\n"
        "- 下一步不明确\n"
        "- 移动端体验\n"
        "- 加载速度\n\n"
        "## 输出要求\n"
        "- 按7个维度逐一分析评分(0-100)\n"
        "- 发现的问题 + 具体改进建议\n"
        "- 快速胜利项（立即实施）\n"
        "- 高影响变更（需要更多投入）\n"
        "- 标题/CTA 替代文案建议\n"
        "- 输出格式：JSON",
        "## 页面类型\n{page_type}\n\n"
        "## 页面URL\n{page_url}\n\n"
        "## 页面内容\n{page_content}\n\n"
        "请对以上页面进行完整的 CRO 分析。"
    ),
    "generate_ai_seo_content": (
        "你是一位 AI 搜索引擎优化(AI SEO/GEO)专家，擅长将内容改写成 AI 系统易于提取和引用的格式。\n\n"
        "## 核心原则\n"
        "AI 系统提取的是段落(passage)而非页面(page)——每个关键主张应当可以独立存在。\n\n"
        "## AI 可提取内容块模式\n\n"
        "### 1. 定义块（回答 'What is X?'）\n"
        "- 40-60字精确定义\n"
        "- 放在章节最前面\n"
        "- 包含核心关键词\n\n"
        "### 2. 步骤块（回答 'How to X?'）\n"
        "- 编号步骤\n"
        "- 每步一个明确动作\n"
        "- 步骤数3-6个为佳\n\n"
        "### 3. 对比块（回答 'X vs Y?'）\n"
        "- 表格格式最佳\n"
        "- 包含特征、价格、适用场景\n\n"
        "### 4. FAQ 块（回答常见问题）\n"
        "- 自然语言问句\n"
        "- 30-50字简洁回答\n"
        "- 适合 FAQ Schema\n\n"
        "### 5. 数据/统计块\n"
        "- 具体数字 + 来源引用\n"
        "- 包含日期（AI 重视时效性）\n\n"
        "## 输出要求\n"
        "- 将原文改写成包含上述块结构的 AI 优化版本\n"
        "- 每个块标注类型和目标查询\n"
        "- 输出格式：JSON",
        "## 原文\n{content}\n\n"
        "## 主题\n{topic}\n\n"
        "## 目标关键词\n{keywords}\n\n"
        "请将以上内容改写成 AI 搜索引擎友好的格式。"
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


# ── SEO/CRO Models ────────────────────────────────────────────────────


class SEOTitleSuggestion(BaseModel):
    """SEO-optimized single title suggestion."""
    title: str = ""
    structure: str = ""
    search_intent: str = ""
    char_count: int = 0
    predicted_ctr: float = 0.0
    reasoning: str = ""


class SEOTitleGenerationResult(BaseModel):
    """SEO title generation result."""
    titles: list[SEOTitleSuggestion] = Field(default_factory=list)
    topic: str = ""
    keywords: str = ""
    search_intent: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class CRODimension(BaseModel):
    """Single CRO analysis dimension."""
    dimension: str = ""
    score: float = 0.0
    issues: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class CROAnalysisReport(BaseModel):
    """Full CRO page analysis result."""
    page_type: str = ""
    page_url: str = ""
    overall_score: float = 0.0
    dimensions: list[CRODimension] = Field(default_factory=list)
    quick_wins: list[str] = Field(default_factory=list)
    high_impact_changes: list[str] = Field(default_factory=list)
    headline_alternatives: list[str] = Field(default_factory=list)
    cta_alternatives: list[str] = Field(default_factory=list)


class AISEOContentBlock(BaseModel):
    """Single AI-extractable content block."""
    block_type: str = ""  # definition / step_by_step / comparison / faq / statistic
    content: str = ""
    target_query: str = ""


class AIOptimizedContentResult(BaseModel):
    """Content structured for AI search extractability."""
    blocks: list[AISEOContentBlock] = Field(default_factory=list)
    original_content: str = ""
    topic: str = ""
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

    # ── SEO/CRO Methods ────────────────────────────────────────────────

    async def generate_seo_titles(
        self,
        topic: str,
        keywords: str = "",
        search_intent: str = "info",
        platform: str = "通用",
        context: str = "",
        count: int = 5,
        temperature: float = 0.7,
    ) -> SEOTitleGenerationResult:
        """生成 SEO 优化的标题候选。

        SEO 标题比社交标题更注重：
        - 关键词前置和密度
        - 搜索意图匹配
        - 字符数控制 (50-60)
        - E-E-A-T 信号

        Args:
            topic: 主题关键词
            keywords: 主关键词和长尾词，逗号分隔
            search_intent: 搜索意图 (info/commercial/transactional/navigational)
            platform: 目标平台（Google/Bing 等）
            context: 额外上下文（品牌名、目标受众等）
            count: 生成数量
            temperature: LLM 温度

        Returns:
            SEOTitleGenerationResult with SEO-optimized title suggestions
        """
        sys_prompt, user_template = _PROMPTS["generate_seo_titles"]
        user_content = user_template.format(
            topic=topic,
            keywords=keywords or topic,
            search_intent=search_intent,
            platform=platform,
            context=context or "暂无",
            count=count,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
        )

        suggestions = self._parse_seo_title_suggestions(raw)
        return SEOTitleGenerationResult(
            titles=suggestions[:count],
            topic=topic,
            keywords=keywords,
            search_intent=search_intent,
        )

    async def analyze_cro_page(
        self,
        page_content: str,
        page_type: str = "landing",
        page_url: str = "",
        temperature: float = 0.7,
    ) -> CROAnalysisReport:
        """对营销页面进行 CRO 分析。

        Analyzes a marketing page across 7 CRO dimensions:
        1. Value Proposition Clarity
        2. Headline Effectiveness
        3. CTA Hierarchy
        4. Visual Hierarchy
        5. Trust Signals
        6. Objection Handling
        7. Friction Points

        Args:
            page_content: 页面内容文本
            page_type: 页面类型 (homepage/landing/pricing/feature/blog)
            page_url: 页面 URL
            temperature: LLM 温度

        Returns:
            CROAnalysisReport with dimensional breakdown
        """
        sys_prompt, user_template = _PROMPTS["analyze_cro"]
        user_content = user_template.format(
            page_type=page_type,
            page_url=page_url or "未提供",
            page_content=page_content,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=4096,
        )

        return self._parse_cro_report(raw, page_type, page_url)

    async def generate_ai_seo_content(
        self,
        content: str,
        topic: str,
        keywords: str = "",
        temperature: float = 0.7,
    ) -> AIOptimizedContentResult:
        """将内容改写为 AI 搜索引擎友好的格式。

        Restructures content into extractable blocks:
        - Definition blocks (40-60 words)
        - Step-by-step blocks
        - Comparison blocks
        - FAQ blocks
        - Data/statistic blocks

        Args:
            content: 原文内容
            topic: 主题
            keywords: 目标关键词
            temperature: LLM 温度

        Returns:
            AIOptimizedContentResult with structured blocks
        """
        sys_prompt, user_template = _PROMPTS["generate_ai_seo_content"]
        user_content = user_template.format(
            content=content,
            topic=topic,
            keywords=keywords or topic,
        )

        raw = await self._call(
            system_prompt=sys_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=4096,
        )

        blocks = self._parse_ai_seo_blocks(raw)
        return AIOptimizedContentResult(
            blocks=blocks,
            original_content=content,
            topic=topic,
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

    def _parse_seo_title_suggestions(self, raw: str) -> list[SEOTitleSuggestion]:
        """Parse LLM output into SEOTitleSuggestion list."""
        parsed = _parse_json_array(raw)
        suggestions: list[SEOTitleSuggestion] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = item.get("title", item.get("text", ""))
            if not title:
                continue
            suggestions.append(SEOTitleSuggestion(
                title=title,
                structure=item.get("structure", item.get("type", "")),
                search_intent=item.get("search_intent", item.get("intent", "")),
                char_count=int(item.get("char_count", item.get("length", 0))),
                predicted_ctr=float(item.get("predicted_ctr", item.get("score", 0))),
                reasoning=item.get("reasoning", item.get("reason", "")),
            ))

        # Fallback: line-by-line
        if not suggestions:
            for line in raw.strip().split("\n"):
                line = line.strip().strip("-*").strip()
                if line and len(line) > 5:
                    suggestions.append(SEOTitleSuggestion(title=line[:100]))

        return suggestions

    def _parse_cro_report(
        self,
        raw: str,
        page_type: str,
        page_url: str,
    ) -> CROAnalysisReport:
        """Parse LLM output into CROAnalysisReport."""
        parsed = _parse_json_array(raw)
        dimensions: list[CRODimension] = []
        quick_wins: list[str] = []
        high_impact: list[str] = []
        headlines: list[str] = []
        ctas: list[str] = []
        overall = 0.0

        for item in parsed:
            if not isinstance(item, dict):
                continue
            dim_type = item.get("type", item.get("dimension", ""))
            if dim_type in ("overall", "summary"):
                overall = float(item.get("score", item.get("overall_score", 0)))
                quick_wins = item.get("quick_wins", item.get("quick_wins", []))
                high_impact = item.get("high_impact", item.get("high_impact_changes", []))
                headlines = item.get("headline_alternatives", item.get("headlines", []))
                ctas = item.get("cta_alternatives", item.get("ctas", []))
            elif dim_type:
                dimensions.append(CRODimension(
                    dimension=dim_type,
                    score=float(item.get("score", 0)),
                    issues=item.get("issues", item.get("problems", [])),
                    recommendations=item.get("recommendations", item.get("suggestions", [])),
                ))

        return CROAnalysisReport(
            page_type=page_type,
            page_url=page_url,
            overall_score=overall,
            dimensions=dimensions,
            quick_wins=quick_wins if isinstance(quick_wins, list) else [],
            high_impact_changes=high_impact if isinstance(high_impact, list) else [],
            headline_alternatives=headlines if isinstance(headlines, list) else [],
            cta_alternatives=ctas if isinstance(ctas, list) else [],
        )

    def _parse_ai_seo_blocks(self, raw: str) -> list[AISEOContentBlock]:
        """Parse LLM output into AISEOContentBlock list."""
        parsed = _parse_json_array(raw)
        blocks: list[AISEOContentBlock] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            block_type = item.get("block_type", item.get("type", ""))
            content = item.get("content", item.get("text", ""))
            if not block_type or not content:
                continue
            blocks.append(AISEOContentBlock(
                block_type=block_type,
                content=content,
                target_query=item.get("target_query", item.get("query", "")),
            ))

        # If we got a single object with a blocks field, use that
        if not blocks and parsed:
            first = parsed[0]
            if isinstance(first, dict) and "blocks" in first:
                for b in first["blocks"]:
                    if isinstance(b, dict):
                        blocks.append(AISEOContentBlock(
                            block_type=b.get("block_type", b.get("type", "")),
                            content=b.get("content", b.get("text", "")),
                            target_query=b.get("target_query", b.get("query", "")),
                        ))

        return blocks


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
