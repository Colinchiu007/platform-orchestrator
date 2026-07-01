"""Tests for viral API routes — uses FastAPI TestClient with mocked services."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import create_app
from middleware.auth import get_current_user

# ── Override auth for testing ───────────────────────────────────────────

TEST_USER = {"sub": "test-user-uuid", "username": "testuser", "tier": 99}


def _override_get_current_user():
    return TEST_USER


app = create_app()
app.dependency_overrides[get_current_user] = _override_get_current_user
client = TestClient(app)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_analyzer():
    """Patch ViralFactorAnalyzer for deterministic results."""
    with patch("routers.viral.create_analyzer") as mock_factory:
        mock_analyzer = mock_factory.return_value

        def fake_analyze_article(data):
            from shared_models.viral import (
                ArticleViralProfile,
                EngagementMetrics,
                TitleAnalysis,
                ViralFactor,
            )
            return ArticleViralProfile(
                title=data.get("title", ""),
                platform_code=data.get("platform_code", "test"),
                overall_score=75.0,
                rank=data.get("rank", 0),
                title_analysis=TitleAnalysis(title=data.get("title", "")),
                engagement=EngagementMetrics(
                    likes=data.get("like_count", 0),
                    comments=data.get("comment_count", 0),
                    total_engagement=data.get("like_count", 0) + data.get("comment_count", 0),
                    viral_score=75.0,
                ),
                factors=[
                    ViralFactor(name="title_structure", label="标题结构", score=0.9),
                    ViralFactor(name="engagement", label="互动热度", score=0.7),
                ],
            )

        mock_analyzer.analyze_article = fake_analyze_article

        def fake_analyze_topic(topic, articles):
            from shared_models.viral import ViralAnalysisResult, ViralFactor
            return ViralAnalysisResult(
                topic=topic,
                overall_score=72.0,
                trend_direction="rising",
                articles=[fake_analyze_article(a) for a in articles],
                platform_scores={"xiaohongshu": 75.0, "douyin": 68.0},
                factors=[ViralFactor(name="title_structure", label="标题结构", score=0.85)],
                suggested_structures=[
                    {"structure": "数字列表", "expected_lift": "2.3x"},
                    {"structure": "悬念式", "expected_lift": "1.8x"},
                ],
                suggested_angles=["AI工具: 入门到精通", "AI工具: N个实用技巧"],
            )

        mock_analyzer.analyze_topic = fake_analyze_topic

        def fake_trending_insights(articles, platform=""):
            from shared_models.viral import TrendingInsights
            return TrendingInsights(
                platform_code=platform,
                total_items=len(articles),
                category_distribution={"tech": 2},
                title_structure_distribution={"numbered_list": 1, "question": 1},
                emotion_distribution={"curiosity": 2},
                top_topics=[{"topic": "AI", "score": 85}],
                rising_keywords=[{"word": "AI工具", "frequency": 3, "signal": "高频"}],
            )

        mock_analyzer.get_trending_insights = fake_trending_insights

        yield mock_analyzer


@pytest.fixture
def mock_generator():
    """Patch ViralCopyGenerator for deterministic results."""
    with patch("routers.viral.create_generator") as mock_factory:
        mock_gen = mock_factory.return_value

        async def fake_generate_titles(**kwargs):
            from services.viral_generator import TitleGenerationResult, TitleSuggestion
            return TitleGenerationResult(
                titles=[
                    TitleSuggestion(title="测试标题1", structure="how_to", predicted_score=85),
                    TitleSuggestion(title="测试标题2", structure="question", predicted_score=72),
                ],
                topic=kwargs.get("topic", ""),
                platform=kwargs.get("platform", ""),
            )

        async def fake_generate_hooks(**kwargs):
            from services.viral_generator import HookGenerationResult, HookSuggestion
            return HookGenerationResult(
                hooks=[
                    HookSuggestion(hook="这是一个Hook", technique="悬念"),
                ],
                title=kwargs.get("title", ""),
                platform=kwargs.get("platform", ""),
            )

        async def fake_rewrite_content(**kwargs):
            from services.viral_generator import RewriteResult
            return RewriteResult(
                rewritten_content="改写后的内容...",
                platform=kwargs.get("platform", ""),
                style=kwargs.get("style", ""),
                original_word_count=100,
                rewritten_word_count=120,
            )

        mock_gen.generate_titles = fake_generate_titles
        mock_gen.generate_hooks = fake_generate_hooks
        mock_gen.rewrite_content = fake_rewrite_content

        yield mock_gen


# ── Tests ───────────────────────────────────────────────────────────────


class TestAnalyzeEndpoint:
    """POST /api/viral/analyze"""

    def test_single_article(self, mock_analyzer):
        resp = client.post("/api/viral/analyze", json={
            "articles": [{"title": "测试文章", "like_count": 100, "comment_count": 20}],
            "topic": "测试",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_score"] == 75.0
        assert data["article_count"] == 1
        assert len(data["factors"]) == 2

    def test_multi_article(self, mock_analyzer):
        resp = client.post("/api/viral/analyze", json={
            "articles": [
                {"title": "文章1", "like_count": 100, "comment_count": 10},
                {"title": "文章2", "like_count": 200, "comment_count": 30},
            ],
            "topic": "AI工具",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_score"] == 72.0
        assert data["article_count"] == 2
        assert "xiaohongshu" in data["platform_scores"]
        assert len(data["suggested_angles"]) >= 2

    def test_empty_articles(self):
        resp = client.post("/api/viral/analyze", json={"articles": [], "topic": "test"})
        assert resp.status_code == 422  # Validation error, min_length=1

    def test_no_auth(self, mock_analyzer):
        """Test with a fresh app that has no auth override."""
        from main import create_app
        app2 = create_app()
        # Remove dependency override to test auth rejection
        client2 = TestClient(app2)
        resp = client2.post("/api/viral/analyze", json={
            "articles": [{"title": "test", "like_count": 1, "comment_count": 1}],
            "topic": "test",
        })
        assert resp.status_code == 401  # jwt returns 401


class TestGenerateEndpoint:
    """POST /api/viral/generate"""

    def test_generate_titles(self, mock_generator):
        resp = client.post("/api/viral/generate", json={
            "topic": "AI工具推荐",
            "platform": "小红书",
            "task": "titles",
            "count": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"] == "titles"
        assert data["topic"] == "AI工具推荐"
        assert len(data["data"]["titles"]) == 2
        assert data["data"]["titles"][0]["title"] == "测试标题1"

    def test_generate_hooks(self, mock_generator):
        resp = client.post("/api/viral/generate", json={
            "topic": "AI工具",
            "content": "文章内容...",
            "platform": "抖音",
            "task": "hooks",
            "count": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"] == "hooks"
        assert len(data["data"]["hooks"]) == 1

    def test_generate_rewrite(self, mock_generator):
        resp = client.post("/api/viral/generate", json={
            "topic": "AI工具",
            "content": "需要改写的原文内容...",
            "platform": "小红书",
            "task": "rewrite",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"] == "rewrite"
        assert "rewritten_content" in data["data"]

    def test_generate_rewrite_no_content(self):
        resp = client.post("/api/viral/generate", json={
            "topic": "AI工具",
            "platform": "小红书",
            "task": "rewrite",
        })
        assert resp.status_code == 400  # router returns 400 explicitly

    def test_unknown_task(self):
        resp = client.post("/api/viral/generate", json={
            "topic": "test",
            "task": "unknown_task",
        })
        assert resp.status_code == 400

    def test_generate_no_topic(self):
        resp = client.post("/api/viral/generate", json={
            "topic": "",
            "task": "titles",
        })
        assert resp.status_code == 422  # min_length=1


class TestTrendingEndpoint:
    """POST /api/viral/trending"""

    def test_trending_with_articles(self, mock_analyzer):
        resp = client.post("/api/viral/trending", json={
            "articles": [
                {"title": "文章1", "like_count": 100, "comment_count": 10},
                {"title": "文章2", "like_count": 200, "comment_count": 30},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_items"] == 2
        assert "tech" in data["category_distribution"]
        assert len(data["rising_keywords"]) >= 1

    def test_trending_empty(self):
        resp = client.post("/api/viral/trending", json={"articles": []})
        assert resp.status_code == 200
        assert resp.json()["total_items"] == 0
