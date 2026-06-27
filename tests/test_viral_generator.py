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