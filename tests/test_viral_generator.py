"""Tests for ViralCopyGenerator — all LLM calls are mocked."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

from services.viral_generator import (
    ViralCopyGenerator,
    TitleSuggestion,
    TitleGenerationResult,
    HookGenerationResult,
    RewriteResult,
    StructureSuggestionResult,
    SEOTitleSuggestion,
    SEOTitleGenerationResult,
    CROAnalysisReport,
    CRODimension,
    AIOptimizedContentResult,
    AISEOContentBlock,
    _call_llm,
    _parse_json_array,
    create_generator,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    """Patch _call_llm to return controlled JSON."""
    with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as mock:
        mock.return_value = json.dumps([
            {
                "title": "10个你不知道的AI工具技巧",
                "structure": "numbered_list",
                "emotion": "curiosity",
                "predicted_score": 85,
                "reasoning": "数字列表+好奇缺口，高互动组合",
            },
            {
                "title": "为什么AI不会取代你，但会用AI的人会",
                "structure": "question",
                "emotion": "anxiety",
                "predicted_score": 78,
                "reasoning": "疑问句+焦虑触发，引发点击",
            },
            {
                "title": "手把手教你用ChatGPT一天赚1000块",
                "structure": "how_to",
                "emotion": "inspiration",
                "predicted_score": 82,
                "reasoning": "How-to+利益承诺，实用型爆款",
            },
        ])
        yield mock


@pytest.fixture
def generator():
    return create_generator(
        api_key="test-key",
        base_url="https://test.api/v1",
        model="test-model",
    )


# ── Unit Tests ──────────────────────────────────────────────────────────


class TestParseJsonArray:
    """Test the resilient JSON array parser."""

    def test_direct_array(self):
        text = '[{"a": 1}, {"a": 2}]'
        assert _parse_json_array(text) == [{"a": 1}, {"a": 2}]

    def test_markdown_fenced(self):
        text = "```json\n[{\"a\": 1}]\n```"
        assert _parse_json_array(text) == [{"a": 1}]

    def test_embedded_array(self):
        text = "Here's the result:\n[{\"a\": 1}]\n---"
        assert _parse_json_array(text) == [{"a": 1}]

    def test_object_extraction(self):
        text = "Result 1: { \"a\": 1 }\nResult 2: { \"a\": 2 }"
        result = _parse_json_array(text)
        assert len(result) == 2
        assert result[0]["a"] == 1

    def test_empty_input(self):
        assert _parse_json_array("") == []

    def test_broken_json(self):
        assert _parse_json_array("{definitely not json}") == []


class TestGeneratorInit:
    """Test generator factory and initialization."""

    def test_create_with_overrides(self):
        gen = create_generator(api_key="k", base_url="https://x.com/v1", model="m")
        assert gen._api_key == "k"
        assert gen._base_url == "https://x.com/v1"
        assert gen._model == "m"

    def test_create_no_key_raises(self):
        gen = create_generator(api_key="", base_url="https://x.com/v1", model="m")
        # No ValueError on init — only on _call
        assert gen._api_key == ""

    def test_create_no_key_on_call(self, generator):
        gen = create_generator(api_key="", base_url="", model="")
        with pytest.raises(ValueError, match="No LLM API key configured"):
            import asyncio
            asyncio.run(gen._call("sys", "user"))


class TestGenerateTitles:
    """Test title generation."""

    @pytest.mark.asyncio
    async def test_generate_titles_basic(self, mock_llm, generator):
        result = await generator.generate_titles(
            topic="AI工具",
            platform="小红书",
            count=3,
        )
        assert isinstance(result, TitleGenerationResult)
        assert len(result.titles) == 3
        assert result.topic == "AI工具"
        assert result.platform == "小红书"

        # Check first title
        t = result.titles[0]
        assert isinstance(t, TitleSuggestion)
        assert t.title == "10个你不知道的AI工具技巧"
        assert t.structure == "numbered_list"
        assert t.predicted_score == 85

    @pytest.mark.asyncio
    async def test_generate_titles_with_analysis(self, mock_llm, generator):
        from shared_models.viral import ViralAnalysisResult
        analysis = ViralAnalysisResult(
            topic="AI工具",
            overall_score=78.5,
            trend_direction="rising",
        )
        result = await generator.generate_titles(
            topic="AI工具",
            analysis=analysis,
            platform="抖音",
            count=2,
        )
        assert len(result.titles) == 2

    @pytest.mark.asyncio
    async def test_generate_titles_empty_response(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = ""
            result = await generator.generate_titles(topic="test", count=3)
            assert len(result.titles) == 0

    @pytest.mark.asyncio
    async def test_generate_titles_fallback_parse(self, generator):
        """When JSON parsing fails, fall back to line-by-line."""
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = "1. 标题一\n2. 标题二\n3. 标题三"
            result = await generator.generate_titles(topic="test", count=3)
            assert len(result.titles) >= 1  # fallback captures what it can


class TestGenerateHooks:
    """Test hook generation."""

    @pytest.mark.asyncio
    async def test_generate_hooks(self, mock_llm, generator):
        result = await generator.generate_hooks(
            title="AI工具推荐",
            content="这是一篇关于AI工具的文章...",
            platform="公众号",
            count=3,
        )
        assert isinstance(result, HookGenerationResult)
        assert result.title == "AI工具推荐"
        assert result.platform == "公众号"

    @pytest.mark.asyncio
    async def test_generate_hooks_no_content(self, mock_llm, generator):
        result = await generator.generate_hooks(
            title="测试标题",
            platform="抖音",
            count=2,
        )
        assert result.title == "测试标题"
        assert result.platform == "抖音"


class TestRewriteContent:
    """Test content rewriting."""

    @pytest.mark.asyncio
    async def test_rewrite_content(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = "这是改写后的内容..."
            result = await generator.rewrite_content(
                content="这是原文内容。包含一些信息需要改写。",
                platform="小红书",
                style="轻松易懂",
            )
            assert isinstance(result, RewriteResult)
            assert result.rewritten_content == "这是改写后的内容..."
            assert result.platform == "小红书"
            assert result.style == "轻松易懂"
            assert result.original_word_count == 16  # 16 CJK chars

    @pytest.mark.asyncio
    async def test_rewrite_content_empty(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = ""
            result = await generator.rewrite_content(
                content="原文",
                platform="通用",
            )
            assert result.rewritten_content == ""
            assert result.original_word_count > 0


class TestSuggestStructures:
    """Test structure suggestion."""

    @pytest.mark.asyncio
    async def test_suggest_structures(self, mock_llm, generator):
        result = await generator.suggest_structures(
            topic="AI工具推荐",
            platform="小红书",
        )
        assert isinstance(result, StructureSuggestionResult)
        assert result.topic == "AI工具推荐"
        assert result.platform == "小红书"


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_llm_http_error(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            import httpx
            mock_req = httpx.Request("POST", "https://test.api/chat/completions")
            mock_resp = httpx.Response(403, request=mock_req)
            m.side_effect = httpx.HTTPStatusError(
                "403 Forbidden",
                request=mock_req,
                response=mock_resp,
            )
            with pytest.raises(httpx.HTTPStatusError):
                await generator.generate_titles(topic="test")

    @pytest.mark.asyncio
    async def test_llm_timeout(self, generator):
        import httpx
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.side_effect = httpx.TimeoutException("Timeout")
            with pytest.raises(httpx.TimeoutException):
                await generator.generate_titles(topic="test")

    def test_count_words(self):
        from services.viral_generator import _count_words
        # CJK only
        assert _count_words("你好世界") == 4
        # English only
        assert _count_words("hello world") == 2
        # Mixed
        assert _count_words("你好world") == 3
        # Empty
        assert _count_words("") == 0
        # Numbers
        assert _count_words("123个") == 1


# ── SEO/CRO Tests ────────────────────────────────────────────────────


SEO_TITLES_JSON = json.dumps([
    {
        "title": "AI 编程工具推荐：10个提升10倍效率的必备插件",
        "structure": "numbered_list",
        "search_intent": "commercial",
        "char_count": 28,
        "predicted_ctr": 82,
        "reasoning": "数字列表+利益承诺，商业意图匹配",
    },
    {
        "title": "如何用 AI 工具自动化日常工作（2026指南）",
        "structure": "how_to",
        "search_intent": "info",
        "char_count": 26,
        "predicted_ctr": 75,
        "reasoning": "How-to+年份限定，信息意图匹配",
    },
    {
        "title": "2026年最佳 AI 编程助手对比：Cursor vs Copilot vs Windsurf",
        "structure": "comparison",
        "search_intent": "commercial",
        "char_count": 32,
        "predicted_ctr": 88,
        "reasoning": "对比+年份，高商业价值",
    },
])

CRO_REPORT_JSON = json.dumps([
    {
        "type": "overall",
        "score": 65,
        "quick_wins": [
            "CTA 按钮文案从'提交'改为'免费获取报告'",
            "表单字段从 8 个减少到 4 个",
        ],
        "high_impact_changes": [
            "头图替换为结果展示型图片",
            "增加客户 logo 行和推荐语",
        ],
        "headline_alternatives": [
            "30天内提升转化率200%——我们的客户证明了这一点",
            "不用代码，不用设计，你的落地页就能转化翻倍",
        ],
        "cta_alternatives": [
            "免费获取完整报告",
            "立即开始30天试用",
        ],
    },
    {
        "dimension": "value_proposition_clarity",
        "score": 55,
        "issues": ["价值主张不清晰，5秒内无法理解产品是什么"],
        "recommendations": ["将'AI驱动'改为具体解决方案描述"],
    },
    {
        "dimension": "headline_effectiveness",
        "score": 70,
        "issues": ["标题有吸引力但与搜索来源不匹配"],
        "recommendations": ["添加数字和具体成果词"],
    },
    {
        "dimension": "cta_hierarchy",
        "score": 45,
        "issues": ["只有一个通用CTA按钮"],
        "recommendations": ["增加主要/次要CTA层级"],
    },
])

AI_SEO_BLOCKS_JSON = json.dumps([
    {
        "block_type": "definition",
        "content": "AI SEO 是优化内容使其在 AI 搜索引擎中更易被提取和引用的实践方法。与传统 SEO 关注排名不同，AI SEO 关注内容的可提取性和可引用性。",
        "target_query": "什么是 AI SEO",
    },
    {
        "block_type": "step_by_step",
        "content": "1. 识别目标查询\n2. 创建40-60字的精确定义块\n3. 添加引用来源和统计数据的支持段落\n4. 使用 FAQ Schema 标记常见问题\n5. 定期更新内容保持时效性",
        "target_query": "如何优化 AI SEO",
    },
    {
        "block_type": "faq",
        "content": "AI SEO 和传统 SEO 有什么区别？传统 SEO 以排名为目标，AI SEO 以被 AI 系统引用为目标。",
        "target_query": "AI SEO vs 传统 SEO",
    },
])


class TestSEOTitles:
    """Test SEO-optimized title generation."""

    @pytest.mark.asyncio
    async def test_generate_seo_titles_basic(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = SEO_TITLES_JSON
            result = await generator.generate_seo_titles(
                topic="AI 编程工具",
                keywords="AI编程, 开发工具, 效率提升",
                search_intent="commercial",
                count=3,
            )
            assert isinstance(result, SEOTitleGenerationResult)
            assert len(result.titles) == 3
            assert result.topic == "AI 编程工具"
            assert result.keywords == "AI编程, 开发工具, 效率提升"
            assert result.search_intent == "commercial"

            # Check first SEO title
            t = result.titles[0]
            assert isinstance(t, SEOTitleSuggestion)
            assert t.title == "AI 编程工具推荐：10个提升10倍效率的必备插件"
            assert t.structure == "numbered_list"
            assert t.search_intent == "commercial"
            assert t.char_count == 28
            assert t.predicted_ctr == 82

    @pytest.mark.asyncio
    async def test_generate_seo_titles_defaults(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = SEO_TITLES_JSON
            result = await generator.generate_seo_titles(
                topic="AI 工具",
            )
            assert len(result.titles) >= 1
            assert result.search_intent == "info"

    @pytest.mark.asyncio
    async def test_generate_seo_titles_empty(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = ""
            result = await generator.generate_seo_titles(topic="test")
            assert len(result.titles) == 0


class TestCROAnalysis:
    """Test CRO page analysis."""

    @pytest.mark.asyncio
    async def test_analyze_cro_page_basic(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = CRO_REPORT_JSON
            result = await generator.analyze_cro_page(
                page_content="<html><body><h1>Welcome</h1><p>AI产品介绍</p></body></html>",
                page_type="landing",
                page_url="https://example.com",
            )
            assert isinstance(result, CROAnalysisReport)
            assert result.page_type == "landing"
            assert result.page_url == "https://example.com"
            assert result.overall_score == 65
            assert len(result.dimensions) == 3
            assert len(result.quick_wins) == 2
            assert len(result.high_impact_changes) == 2
            assert len(result.headline_alternatives) == 2
            assert len(result.cta_alternatives) == 2

    @pytest.mark.asyncio
    async def test_analyze_cro_dimension_order(self, generator):
        """Verify CRO dimensions retain correct names from JSON."""
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = CRO_REPORT_JSON
            result = await generator.analyze_cro_page(
                page_content="<h1>Product</h1>",
                page_type="homepage",
            )
            assert result.dimensions[0].dimension == "value_proposition_clarity"
            assert result.dimensions[0].score == 55
            assert len(result.dimensions[0].issues) == 1

    @pytest.mark.asyncio
    async def test_analyze_cro_empty_response(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = "[]"
            result = await generator.analyze_cro_page(
                page_content="test",
                page_type="blog",
            )
            assert result.overall_score == 0
            assert len(result.dimensions) == 0


class TestAISEOContent:
    """Test AI-optimized content generation."""

    @pytest.mark.asyncio
    async def test_generate_ai_seo_content_basic(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = AI_SEO_BLOCKS_JSON
            result = await generator.generate_ai_seo_content(
                content="AI SEO is the practice of optimizing content...",
                topic="AI SEO 入门",
                keywords="AI SEO, GEO, LLMO",
            )
            assert isinstance(result, AIOptimizedContentResult)
            assert len(result.blocks) == 3
            assert result.topic == "AI SEO 入门"
            assert result.original_content == "AI SEO is the practice of optimizing content..."

            # Check block types
            assert result.blocks[0].block_type == "definition"
            assert result.blocks[1].block_type == "step_by_step"
            assert result.blocks[2].block_type == "faq"

    @pytest.mark.asyncio
    async def test_generate_ai_seo_content_empty_response(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = ""
            result = await generator.generate_ai_seo_content(
                content="test",
                topic="test",
            )
            assert len(result.blocks) == 0

    @pytest.mark.asyncio
    async def test_ai_seo_block_has_query(self, generator):
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = AI_SEO_BLOCKS_JSON
            result = await generator.generate_ai_seo_content(
                content="test content",
                topic="test",
            )
            assert result.blocks[0].target_query == "什么是 AI SEO"
            assert result.blocks[1].target_query == "如何优化 AI SEO"


class TestSEOEdgeCases:
    """Test edge cases for SEO/CRO methods."""

    @pytest.mark.asyncio
    async def test_seo_titles_fallback_parse(self, generator):
        """When JSON parsing fails, fall back to line-by-line."""
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = "1. SEO标题一\n2. SEO标题二"
            result = await generator.generate_seo_titles(topic="test")
            assert len(result.titles) >= 1

    @pytest.mark.asyncio
    async def test_ai_seo_blocks_single_object(self, generator):
        """Handle single object with blocks field."""
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = json.dumps({
                "blocks": [
                    {"block_type": "definition", "content": "Test block", "target_query": "test"},
                ]
            })
            result = await generator.generate_ai_seo_content(
                content="test",
                topic="test",
            )
            assert len(result.blocks) == 1
            assert result.blocks[0].block_type == "definition"

    @pytest.mark.asyncio
    async def test_cro_dimensions_optional_fields(self, generator):
        """CRO dimensions with missing optional fields should not crash."""
        with patch("services.viral_generator._call_llm", new_callable=AsyncMock) as m:
            m.return_value = json.dumps([
                {"type": "overall", "score": 50},
                {"dimension": "trust_signals", "score": 40},
            ])
            result = await generator.analyze_cro_page(
                page_content="test",
                page_type="pricing",
            )
            assert result.overall_score == 50
            assert len(result.dimensions) == 1
            assert result.dimensions[0].dimension == "trust_signals"
