"""Viral analysis and copy generation API endpoints.

Wraps ViralFactorAnalyzer and ViralCopyGenerator services behind
RESTful endpoints for use by Multi-Publish and standalone web app.

Endpoints:
  POST /api/viral/analyze    — Analyze article(s) for viral factors
  POST /api/viral/generate   — Generate viral copy (titles, hooks, rewrite)
  POST /api/viral/structures — Recommend content structures
  GET  /api/viral/trending   — Platform trending insights
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature
from services.viral_analyzer import create_analyzer
from services.viral_generator import create_generator

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request / Response Models ──────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    """API request for viral factor analysis."""
    articles: list[dict] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="List of article dicts with at least title, like_count, comment_count",
    )
    topic: str = Field(
        default="",
        description="Optional topic label for aggregation",
    )
    platform: Optional[str] = Field(
        default=None,
        description="Filter to a specific platform",
    )


class AnalyzeResponse(BaseModel):
    """API response for viral factor analysis."""
    success: bool = True
    topic: str = ""
    overall_score: float = 0.0
    trend_direction: str = "stable"
    article_count: int = 0
    platform_scores: dict[str, float] = Field(default_factory=dict)
    factors: list[dict] = Field(default_factory=list)
    suggested_structures: list[dict] = Field(default_factory=list)
    suggested_angles: list[str] = Field(default_factory=list)
    rising_keywords: list[dict] = Field(default_factory=list)
    analyzed_at: str = ""


class TrendingRequest(BaseModel):
    """API request for trending insights — allows empty articles for fresh platforms."""
    articles: list[dict] = Field(
        default_factory=list,
        description="Optional list of articles for trending analysis; empty returns base response",
    )
    platform: str = Field(
        default="",
        description="Platform to analyze",
    )


class GenerateRequest(BaseModel):
    """API request for viral copy generation."""
    topic: str = Field(
        ...,
        min_length=1,
        description="Topic or keyword for generation",
    )
    content: str = Field(
        default="",
        description="Original content for rewriting (optional for title gen)",
    )
    platform: str = Field(
        default="通用",
        description="Target platform: 小红书/抖音/公众号/通用",
    )
    task: str = Field(
        default="titles",
        description="Generation task: titles / hooks / rewrite / structures",
    )
    style: str = Field(
        default="自动适配",
        description="Writing style for rewrite: 自动适配/轻松易懂/吸引眼球/深度分析",
    )
    count: int = Field(default=5, ge=1, le=10, description="Number of variants to generate")


class GenerateResponse(BaseModel):
    """API response for viral copy generation."""
    success: bool = True
    task: str = ""
    platform: str = ""
    topic: str = ""
    data: dict = Field(default_factory=dict)
    generated_at: str = ""


class TrendingResponse(BaseModel):
    """API response for trending insights."""
    success: bool = True
    platform: str = ""
    total_items: int = 0
    category_distribution: dict[str, int] = Field(default_factory=dict)
    title_structure_distribution: dict[str, int] = Field(default_factory=dict)
    emotion_distribution: dict[str, int] = Field(default_factory=dict)
    top_topics: list[dict] = Field(default_factory=list)
    rising_keywords: list[dict] = Field(default_factory=list)
    analyzed_at: str = ""


# ── Endpoints ──────────────────────────────────────────────────────────


@router.post("/analyze", response_model=AnalyzeResponse)
@requires_feature("viral_analyze")
async def analyze_articles(
    body: AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Analyze article(s) for viral factors.

    Accepts a list of trending articles and returns:
    - Overall viral score with factor breakdown
    - Platform comparison
    - Title structure & emotion distribution
    - Rising keyword extraction
    - Suggested writing angles
    """
    try:
        analyzer = create_analyzer()
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to initialize analyzer: {e}")

    topic = body.topic or (body.articles[0].get("category") or "general")

    try:
        # Single article quick analysis
        if len(body.articles) == 1:
            profile = analyzer.analyze_article(body.articles[0])
            return AnalyzeResponse(
                topic=topic,
                overall_score=profile.overall_score,
                article_count=1,
                platform_scores={profile.platform_code: profile.overall_score},
                factors=[f.model_dump() for f in profile.factors],
                analyzed_at=datetime.now(timezone.utc).isoformat(),
            )

        # Multi-article topic analysis
        result = analyzer.analyze_topic(topic, body.articles)
        insights = None
        if body.platform:
            insights = analyzer.get_trending_insights(
                body.articles, platform=body.platform
            )

        return AnalyzeResponse(
            topic=result.topic,
            overall_score=result.overall_score,
            trend_direction=result.trend_direction,
            article_count=len(result.articles),
            platform_scores=result.platform_scores,
            factors=[f.model_dump() for f in result.factors],
            suggested_structures=result.suggested_structures,
            suggested_angles=result.suggested_angles,
            rising_keywords=(
                insights.rising_keywords if insights
                else analyzer.get_trending_insights(
                    body.articles
                ).rising_keywords
            ),
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))


@router.post("/generate", response_model=GenerateResponse)
@requires_feature("viral_generate")
async def generate_copy(
    body: GenerateRequest,
    current_user: dict = Depends(get_current_user),
):
    """Generate viral-optimized copy based on analysis.

    Supports four generation tasks:
    - titles: Generate N title variants with predicted scores
    - hooks: Generate opening hooks/attention grabbers
    - rewrite: Rewrite content for specific platform
    - structures: Recommend content structures with outlines

    Uses LLM configured via PO_OPENAI_API_KEY / PO_OPENAI_BASE_URL / PO_OPENAI_MODEL.
    """
    try:
        generator = create_generator()
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to initialize generator: {e}")

    try:
        now = datetime.now(timezone.utc).isoformat()

        if body.task == "titles":
            result = await generator.generate_titles(
                topic=body.topic,
                platform=body.platform,
                count=body.count,
            )
            return GenerateResponse(
                task="titles",
                platform=body.platform,
                topic=body.topic,
                data={"titles": [t.model_dump() for t in result.titles]},
                generated_at=now,
            )

        elif body.task == "hooks":
            result = await generator.generate_hooks(
                title=body.topic,
                content=body.content,
                platform=body.platform,
                count=body.count,
            )
            return GenerateResponse(
                task="hooks",
                platform=body.platform,
                topic=body.topic,
                data={"hooks": [h.model_dump() for h in result.hooks]},
                generated_at=now,
            )

        elif body.task == "rewrite":
            if not body.content:
                raise HTTPException(400, detail="content is required for rewrite task")
            result = await generator.rewrite_content(
                content=body.content,
                platform=body.platform,
                style=body.style,
            )
            return GenerateResponse(
                task="rewrite",
                platform=body.platform,
                topic=body.topic,
                data={
                    "rewritten_content": result.rewritten_content,
                    "original_word_count": result.original_word_count,
                    "rewritten_word_count": result.rewritten_word_count,
                },
                generated_at=now,
            )

        elif body.task == "structures":
            result = await generator.suggest_structures(
                topic=body.topic,
                platform=body.platform,
            )
            return GenerateResponse(
                task="structures",
                platform=body.platform,
                topic=body.topic,
                data={
                    "suggestions": [s.model_dump() for s in result.suggestions]
                },
                generated_at=now,
            )

        else:
            raise HTTPException(
                400,
                detail=f"Unknown task '{body.task}'. Must be one of: titles, hooks, rewrite, structures",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Generation failed: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))


@router.post("/trending", response_model=TrendingResponse)
@requires_feature("viral_analyze")
async def trending_insights(
    body: TrendingRequest,
    current_user: dict = Depends(get_current_user),
):
    """Get aggregated trending insights for a platform.

    Accepts a list of articles and returns:
    - Category distribution
    - Title structure & emotion distribution
    - Top topics by score
    - Rising keywords
    """
    try:
        analyzer = create_analyzer()
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to initialize analyzer: {e}")

    if not body.articles:
        return TrendingResponse(
            platform=body.platform or "all",
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

    try:
        insights = analyzer.get_trending_insights(
            body.articles,
            platform=body.platform or "all",
        )
        return TrendingResponse(
            platform=insights.platform_code or (body.platform or "all"),
            total_items=insights.total_items,
            category_distribution=insights.category_distribution,
            title_structure_distribution=insights.title_structure_distribution,
            emotion_distribution=insights.emotion_distribution,
            top_topics=insights.top_topics,
            rising_keywords=insights.rising_keywords,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        logger.error(f"Trending analysis failed: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))
