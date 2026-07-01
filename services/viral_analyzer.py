#!/usr/bin/env python3
"""ViralFactorAnalyzer — 爆款因子分析引擎

Analyzes trending content to extract viral factors:
  - Title structure classification (question, list, how-to, suspense, etc.)
  - Emotional trigger detection
  - Engagement-based viral scoring (platform-normalized)
  - Keyword heat mapping
  - Content structure pattern analysis

Usage:
    analyzer = ViralFactorAnalyzer()

    # Analyze a single article
    profile = analyzer.analyze_article(article_data)

    # Full topic analysis
    result = analyzer.analyze_topic("AI工具", articles_list)

    # Platform trend insights
    insights = analyzer.get_trending_insights(articles_list, platform="xiaohongshu")

Dependencies:
    - shared-models (ArticleViralProfile, ViralAnalysisResult, TrendingInsights, etc.)
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from shared_models.viral import (
    ArticleViralProfile,
    ContentStructure,
    EmotionalTrigger,
    EngagementMetrics,
    TitleAnalysis,
    TitleStructure,
    TrendingInsights,
    ViralAnalysisResult,
    ViralFactor,
    ViralScoringConfig,
)

logger = logging.getLogger(__name__)


# ── Title structure regex patterns ──────────────────────────────────────

# Priority-ordered: more specific patterns first
_TITLE_PATTERNS: list[tuple[TitleStructure, re.Pattern, str]] = [
    # Numbered list
    (TitleStructure.NUMBERED_LIST, re.compile(r'^\d+[\.\s、]|^\d+\s*(个|种|大|招|步|条|点|款|个方法)|[\(（]\d+[\)）]'), "数字列表"),
    # Question (only pure-question patterns, not How-to)
    (TitleStructure.QUESTION, re.compile(r'[？?]\s*$|^(为什么|是否|有没有|要不要|能不能|该不该)\s'), "疑问句式"),
    # How-to (must come before Question for 如何/怎么 patterns)
    (TitleStructure.HOW_TO, re.compile(r'^(如何|怎么|怎样|教你|学会|手把手|攻略|指南|教程|入门)', re.IGNORECASE), "How-to 教学"),
    # Comparison
    (TitleStructure.COMPARISON, re.compile(r'vs|VS|对比|还是|或者|pk|PK|哪个好|怎么选|[与和]vs'), "对比式"),
    # Timely (with year or season)
    (TitleStructure.TIMELY, re.compile(r'(202[4-9]|20[3-9]\d|今年|最新|趋势|风口|未来|现在)'), "时效式"),
    # Command
    (TitleStructure.COMMAND, re.compile(r'^(千万别|一定要|立刻|马上|赶快|必须|建议|推荐|Warning|注意|警惕)'), "命令式"),
    # Negative
    (TitleStructure.NEGATIVE, re.compile(r'(别再|不要|不行了|翻车|避雷|踩坑|后悔|坑|骗|假|废了)'), "否定式"),
    # Controversy
    (TitleStructure.CONTROVERSY, re.compile(r'(争议|惹怒|吵翻|炸锅|疯了|震惊|竟然|居然|彻底|颠覆)'), "争议式"),
    # Suspense
    (TitleStructure.SUSPENSE, re.compile(r'(没想到|结果|真相|秘密|背后|原来|实情|终于|发现了一个|我悟了)'), "悬念式"),
    # Curiosity gap
    (TitleStructure.CURIOSITY_GAP, re.compile(r'(秘密|隐藏|不为人知|没人告诉你|99%|90%|大多数人|误区)'), "好奇缺口"),
    # Story
    (TitleStructure.STORY, re.compile(r'^(我|我们|朋友|同事|闺蜜|老公|老婆|我妈|我爸|我家|我的)[的了经历了在]'), "故事式"),
    # Direct (default if nothing else matches)
]

# ── Power words ─────



_POWER_WORDS_ZH: list[str] = [
    # Urgency
    "立刻", "马上", "赶紧", "限时", "最后", "紧急",
    # Authority
    "专家", "权威", "官方", "研究", "科学证实", "哈佛", "MIT", "斯坦福",
    # Social proof
    "爆火", "疯抢", "万人", "爆款", "刷屏", "全网",
    # Curiosity
    "秘密", "隐藏", "不为人知", "内部", "独家", "揭秘",
    # Certainty
    "必看", "必收", "必备", "必知", "一定要", "千万别",
    # Benefit
    "免费", "省钱", "省时", "高效", "简单", "轻松", "零基础",
    # Emotion
    "感动", "泪目", "暖心", "震撼", "惊艳", "绝了",
    # Novelty
    "最新", "新款", "首发", "首次", "全新", "升级",
]

_POWER_WORDS_EN: list[str] = [
    "ultimate", "essential", "crucial", "vital", "proven",
    "exclusive", "secret", "hidden", "insider", "never",
    "free", "easy", "simple", "quick", "instant",
    "amazing", "incredible", "unbelievable", "shocking",
    "best", "top", "greatest", "biggest", "worst",
    "you", "your", "why", "how", "because",
]

# ── Emotional trigger keywords ──

_EMOTION_PATTERNS: list[tuple[EmotionalTrigger, list[str]]] = [
    (EmotionalTrigger.SURPRISE, ["震惊", "惊艳", "震撼", "绝了", "神了", "不可思议", "难以置信", "炸了", "惊了"]),
    (EmotionalTrigger.CURIOSITY, ["为什么", "怎么做到的", "原因", "秘密", "真相", "背后", "揭秘", "没想到", "居然", "竟然", "结果"]),
    (EmotionalTrigger.CONTROVERSY, ["争议", "惹怒", "吵翻", "滚出", "道歉", "翻车", "避雷", "踩坑", "别再", "骗"]),
    (EmotionalTrigger.EMPATHY, ["谁懂", "懂的都懂", "共鸣", "同感", "理解", "不容易", "心疼", "泪目", "当妈", "过来人"]),
    (EmotionalTrigger.ANXIETY, ["危险", "警告", "注意", "紧急", "风险", "小心", "淘汰", "危机", "失业", "焦虑", "裁"]),
    (EmotionalTrigger.FEAR, ["可怕", "恐怖", "吓", "死亡", "出事", "封号", "坐牢", "没了"]),
    (EmotionalTrigger.ANGER, ["恶心", "过分", "离谱", "无语", "垃圾", "坑", "愤怒", "忍不了", "投诉", "举报"]),
    (EmotionalTrigger.INSPIRATION, ["改变", "成长", "蜕变", "突破", "坚持", "自律", "逆袭", "翻身", "重启", "觉醒"]),
    (EmotionalTrigger.JOY, ["开心", "快乐", "幸福", "治愈", "温暖", "美好", "可爱", "笑容", "有趣", "甜甜"]),
]

# ── Content structure patterns ──

_CONTENT_STRUCTURE_PATTERNS: list[tuple[ContentStructure, list[str]]] = [
    (ContentStructure.LIST, ["\n1.", "\n2.", "\n3.", "第一", "第二", "第三", "首先", "其次", "最后", "①", "②", "③"]),
    (ContentStructure.STORY, ["我", "我们", "去年", "当时", "记得", "有一次", "那天", "曾经"]),
    (ContentStructure.TUTORIAL, ["步骤", "教程", "教你", "方法", "技巧", "如何", "怎么", "攻略"]),
    (ContentStructure.OPINION, ["我觉得", "我认为", "说实话", "坦白说", "讲真", "个人认为", "我的观点"]),
    (ContentStructure.EMOTIONAL, ["感动", "泪目", "哭了", "心疼", "太", "真的", "超级", "绝了"]),
    (ContentStructure.NEWS, ["据报道", "据悉", "月", "日", "消息", "发布", "宣布", "官宣"]),
    (ContentStructure.REVIEW, ["测评", "评测", "体验", "使用感受", "开箱", "实测", "打卡"]),
    (ContentStructure.GUIDE, ["指南", "全攻略", "合集", "清单", "地图", "路线", "方案", "计划"]),
]


# ── Analyzer ──────────────────────────────────────────────────────────────


class ViralFactorAnalyzer:
    """爆款因子分析引擎 — 核心分析器。

    Stateless by design: receives data, returns analysis. No internal cache or state.
    Thread-safe (no mutable class state).
    """

    def __init__(self, config: Optional[ViralScoringConfig] = None) -> None:
        self.config = config or ViralScoringConfig()

    # ── Public API ──────────────────────────────────────────────────────

    def analyze_article(self, article: dict | Any, platform: str = "") -> ArticleViralProfile:
        """Analyze a single article/trending item for viral factors.

        Args:
            article: Data dict or HotArticleModel-like object with fields:
                     title, like_count, comment_count, share_count, summary, etc.
            platform: Platform code override (reads from article if not provided)

        Returns:
            ArticleViralProfile with all factor dimensions extracted.
        """
        # Normalize to dict
        if not isinstance(article, dict):
            article = self._to_dict(article)

        platform = platform or article.get("platform_code", "")
        title = article.get("title", "")
        summary = article.get("summary", "") or article.get("content_text", "")

        # Title analysis
        title_analysis = self._analyze_title(title)

        # Engagement metrics
        engagement = self._compute_engagement(article, platform)

        # Factors
        factors = self._extract_factors(title, summary, engagement)

        # Overall score
        overall_score = engagement.viral_score

        return ArticleViralProfile(
            platform_code=platform,
            title=title,
            author_name=article.get("author_name", ""),
            author_followers=article.get("author_followers", 0),
            source_url=article.get("source_url", ""),
            category=article.get("category", "general"),
            title_analysis=title_analysis,
            engagement=engagement,
            factors=factors,
            overall_score=round(overall_score, 1),
            rank=article.get("rank", 0),
            snapshot_at=article.get("snapshot_at") or datetime.now(timezone.utc),
        )

    def analyze_topic(
        self,
        topic: str,
        articles: list[dict | Any],
    ) -> ViralAnalysisResult:
        """Analyze a topic across multiple articles.

        Aggregates individual article profiles into a topic-level analysis,
        identifies common factors, platform differences, and generates
        writing suggestions.

        Args:
            topic: Topic or keyword to analyze
            articles: List of article data dicts

        Returns:
            ViralAnalysisResult with aggregated analysis and suggestions.
        """
        profiles = [self.analyze_article(a) for a in articles]

        if not profiles:
            return ViralAnalysisResult(topic=topic, overall_score=0.0)

        # Aggregate scores
        avg_score = sum(p.overall_score for p in profiles) / len(profiles)

        # Aggregate factors across all profiles
        factor_scores: dict[str, list[float]] = defaultdict(list)
        for p in profiles:
            for f in p.factors:
                factor_scores[f.name].append(f.score)
        aggregated_factors = [
            ViralFactor(
                name=name,
                label=name,
                score=round(sum(scores) / len(scores), 2),
                confidence=0.7,
            )
            for name, scores in factor_scores.items()
        ]

        # Platform scores
        platform_scores: dict[str, list[float]] = defaultdict(list)
        for p in profiles:
            platform_scores[p.platform_code].append(p.overall_score)
        platform_avg = {
            plat: round(sum(scores) / len(scores), 1)
            for plat, scores in platform_scores.items()
        }

        # Title structure distribution
        structure_counts: dict[str, int] = defaultdict(int)
        for p in profiles:
            structure_counts[p.title_analysis.structure.value] += 1

        # Find best-performing structure
        sorted_structures = sorted(
            structure_counts.items(), key=lambda x: x[1], reverse=True
        )

        # Generate suggestions
        suggested_structures = self._suggest_structures(sorted_structures, profiles)
        suggested_angles = self._suggest_angles(topic, profiles)

        # Trend direction (basic heuristic)
        trend = self._detect_trend(profiles)

        return ViralAnalysisResult(
            topic=topic,
            overall_score=round(avg_score, 1),
            trend_direction=trend,
            confidence=0.6 if len(profiles) >= 3 else 0.3,
            factors=aggregated_factors,
            articles=profiles,
            platform_scores=platform_avg,
            suggested_structures=suggested_structures,
            suggested_angles=suggested_angles,
        )

    def get_trending_insights(
        self,
        articles: list[dict | Any],
        platform: str = "",
    ) -> TrendingInsights:
        """Aggregate a list of articles into platform-level trending insights.

        Args:
            articles: List of article data
            platform: Platform code (auto-detected from first article if empty)

        Returns:
            TrendingInsights with distributions, top topics, rising keywords.
        """
        profiles = [self.analyze_article(a) for a in articles]
        platform = platform or (profiles[0].platform_code if profiles else "")

        # Category distribution
        cat_counts: Counter = Counter(p.category for p in profiles)

        # Title structure distribution
        struct_counts: Counter = Counter(p.title_analysis.structure.value for p in profiles)

        # Emotion distribution
        emotion_counts: Counter = Counter(
            p.title_analysis.emotion.value for p in profiles
        )

        # Top topics (sorted by overall_score)
        top_topics = sorted(
            [
                {
                    "title": p.title,
                    "score": p.overall_score,
                    "platform": p.platform_code,
                    "url": p.source_url,
                    "category": p.category,
                    "structure": p.title_analysis.structure.value,
                }
                for p in profiles
            ],
            key=lambda x: x["score"],
            reverse=True,
        )[:20]

        # Rising keywords
        rising_keywords = self._extract_rising_keywords(profiles)

        return TrendingInsights(
            platform_code=platform,
            total_items=len(profiles),
            category_distribution=dict(cat_counts),
            title_structure_distribution=dict(struct_counts),
            emotion_distribution=dict(emotion_counts),
            top_topics=top_topics,
            rising_keywords=rising_keywords[:30],
        )

    # ── Internal: Title Analysis ────────────────────────────────────────

    def _analyze_title(self, title: str) -> TitleAnalysis:
        """Classify title structure and extract features."""
        if not title:
            return TitleAnalysis(title="", length=0)

        length = len(title)
        word_count = len(title.split())

        # Detect structure
        structure, confidence = self._classify_structure(title)

        # Detect emotion
        emotion, emotion_conf = self._detect_emotion(title)

        # Feature extraction
        has_numbers = bool(re.search(r'\d', title))
        has_questions = bool(re.search(r'[？?]', title)) or bool(re.search(r'^(为什么|如何|怎么|怎样|有没有|要不要|能不能)', title))
        has_emojis = bool(re.search(r'[\U0001F000-\U0001FFFF☀-➿]', title))
        has_colon = '：' in title or ':' in title

        # Power words
        power_words = self._find_power_words(title)

        return TitleAnalysis(
            title=title,
            structure=structure,
            length=length,
            word_count=word_count,
            has_numbers=has_numbers,
            has_questions=has_questions,
            has_emojis=has_emojis,
            has_colon=has_colon,
            has_power_words=power_words,
            emotion=emotion,
            confidence=emotion_conf,
        )

    def _classify_structure(self, title: str) -> tuple[TitleStructure, float]:
        """Classify title by structural patterns. Returns (structure, confidence)."""
        for structure, pattern, _ in _TITLE_PATTERNS:
            if pattern.search(title):
                return structure, 0.8
        return TitleStructure.DIRECT, 0.5

    def _detect_emotion(self, text: str) -> tuple[EmotionalTrigger, float]:
        """Detect primary emotional trigger in text."""
        best_trigger = EmotionalTrigger.NONE
        best_score = 0

        for trigger, keywords in _EMOTION_PATTERNS:
            # Weighted scoring: position matters, earlier keywords are stronger signals
            score = sum(max(0, 3 - i) for i, kw in enumerate(keywords) if kw in text)
            if score > best_score:
                best_score = score
                best_trigger = trigger

        if best_score == 0:
            return EmotionalTrigger.NONE, 0.0

        confidence = min(0.5 + best_score * 0.15, 0.95)
        return best_trigger, confidence

    def _find_power_words(self, title: str) -> list[str]:
        """Find power words present in the title."""
        found = []
        for word in _POWER_WORDS_ZH + _POWER_WORDS_EN:
            if word in title.lower():
                found.append(word)
        return found

    # ── Internal: Engagement Scoring ────────────────────────────────────

    def _compute_engagement(
        self, article: dict, platform: str
    ) -> EngagementMetrics:
        """Compute normalized engagement metrics and viral score."""
        likes = int(article.get("like_count", article.get("likes", 0)))
        comments = int(article.get("comment_count", article.get("comments", 0)))
        shares = int(article.get("share_count", article.get("shares", 0)))
        favorites = int(article.get("favor_count", article.get("favorites", 0)))
        views = int(article.get("read_count", article.get("views", 0)))

        # Platform-specific ceilings for normalization
        ceilings = self.config.platform_ceilings.get(platform, [50000, 10000, 20000])

        def _norm(value: int, ceiling: int) -> float:
            if value <= 0:
                return 0.0
            return min(math.log10(max(value, 1)) / math.log10(max(ceiling, 100)), 1.0) * 100

        likes_norm = _norm(likes, ceilings[0] if len(ceilings) > 0 else 50000)
        comments_norm = _norm(comments, ceilings[1] if len(ceilings) > 1 else 10000)
        shares_norm = _norm(shares, ceilings[2] if len(ceilings) > 2 else 20000)
        favorites_norm = _norm(favorites, max(ceilings[0] if len(ceilings) > 0 else 50000, 1))

        # Weighted viral score
        cfg = self.config
        linear_score = (
            cfg.weight_likes * likes_norm
            + cfg.weight_comments * comments_norm
            + cfg.weight_shares * shares_norm
            + cfg.weight_favorites * favorites_norm
        )

        # Log-scale raw score (dampens outliers)
        total_raw = likes + comments + shares + favorites
        log_score = math.log10(max(total_raw, 1)) * 10

        # Authority bonus (author followers)
        author_followers = int(article.get("author_followers", 0))
        authority_score = min(math.log10(max(author_followers, 1)) / 6, 1.0) * 100

        viral_score = (
            cfg.w_linear * linear_score
            + cfg.w_log * log_score
            + cfg.w_authority * authority_score
        )
        viral_score = min(viral_score, 100.0)

        # Engagement rate (if views available)
        engagement_rate = 0.0
        if views > 0:
            engagement_rate = (likes + comments + shares) / views

        return EngagementMetrics(
            likes=likes,
            comments=comments,
            shares=shares,
            favorites=favorites,
            views=views,
            likes_norm=round(likes_norm, 1),
            comments_norm=round(comments_norm, 1),
            shares_norm=round(shares_norm, 1),
            favorites_norm=round(favorites_norm, 1),
            total_engagement=total_raw,
            engagement_rate=round(engagement_rate, 4),
            viral_score=round(viral_score, 1),
        )

    # ── Internal: Factor Extraction ─────────────────────────────────────

    def _extract_factors(
        self,
        title: str,
        summary: str,
        engagement: EngagementMetrics,
    ) -> list[ViralFactor]:
        """Extract all viral factor dimensions."""
        factors = []

        # 1. Title structure factor
        ta = self._analyze_title(title)
        title_quality = 0.3
        if ta.structure != TitleStructure.DIRECT and ta.structure != TitleStructure.OTHER:
            title_quality = 0.7
        if ta.has_numbers:
            title_quality += 0.1
        if ta.has_power_words:
            title_quality += 0.1
        if ta.emotion != EmotionalTrigger.NONE:
            title_quality += 0.1

        factors.append(ViralFactor(
            name="title_structure",
            label="标题结构",
            score=round(min(title_quality, 1.0), 2),
            confidence=ta.confidence,
            evidence=[f"结构类型: {ta.structure.value}", f"情绪触发: {ta.emotion.value}"]
            if ta.structure != TitleStructure.OTHER and ta.emotion != EmotionalTrigger.NONE
            else ["基础标题结构"],
            details={
                "structure": ta.structure.value,
                "emotion": ta.emotion.value,
                "power_words": ta.has_power_words,
            },
        ))

        # 2. Engagement factor
        eng_score = min(engagement.viral_score / 100, 1.0)
        factors.append(ViralFactor(
            name="engagement",
            label="互动热度",
            score=round(eng_score, 2),
            confidence=0.7 if engagement.total_engagement > 0 else 0.3,
            evidence=[
                f"点赞: {engagement.likes}",
                f"评论: {engagement.comments}",
                f"分享: {engagement.shares}",
            ],
            details={
                "total_engagement": engagement.total_engagement,
                "engagement_rate": engagement.engagement_rate,
            },
        ))

        # 3. Length appropriateness factor
        length = len(title)
        if 10 <= length <= 30:
            length_score = 0.9
        elif 5 <= length <= 40:
            length_score = 0.7
        else:
            length_score = 0.4

        factors.append(ViralFactor(
            name="length",
            label="长度窗口",
            score=round(length_score, 2),
            confidence=0.8,
            evidence=[f"标题长度: {length}字"],
            details={"length": length, "optimal_range": "10-30字"},
        ))

        # 4. Content structure factor (if summary available)
        if summary:
            cs, cs_conf = self._classify_content_structure(summary)
            factors.append(ViralFactor(
                name="content_structure",
                label="内容结构",
                score=round(0.7 if cs != ContentStructure.OTHER else 0.4, 2),
                confidence=round(cs_conf, 2),
                evidence=[f"内容类型: {cs.value}"],
                details={"structure": cs.value},
            ))

        return factors

    def _classify_content_structure(
        self, text: str
    ) -> tuple[ContentStructure, float]:
        """Classify body content structure from text."""
        best = ContentStructure.OTHER
        best_score = 0

        for structure, patterns in _CONTENT_STRUCTURE_PATTERNS:
            score = sum(1 for p in patterns if p in text)
            if score > best_score:
                best_score = score
                best = structure

        if best_score == 0:
            return ContentStructure.OTHER, 0.3

        confidence = min(0.4 + best_score * 0.15, 0.85)
        return best, confidence

    # ── Internal: Aggregation ───────────────────────────────────────────

    def _suggest_structures(
        self,
        sorted_structures: list[tuple[str, int]],
        profiles: list[ArticleViralProfile],
    ) -> list[dict]:
        """Suggest title structures based on performance data."""
        suggestions = []

        # Map structure to average score
        structure_scores: dict[str, list[float]] = defaultdict(list)
        for p in profiles:
            structure_scores[p.title_analysis.structure.value].append(p.overall_score)

        structure_avg = {
            s: round(sum(scores) / len(scores), 1)
            for s, scores in structure_scores.items()
        }

        # Build suggestions with lift estimates
        if structure_avg:
            max_score = max(structure_avg.values()) if structure_avg else 1.0
            for structure, avg in sorted(
                structure_avg.items(), key=lambda x: x[1], reverse=True
            )[:5]:
                lift = round(avg / max_score, 1) if max_score > 0 else 1.0
                suggestions.append({
                    "type": structure,
                    "lift": lift,
                    "expected_score": avg,
                })

        return suggestions

    def _suggest_angles(
        self, topic: str, profiles: list[ArticleViralProfile]
    ) -> list[str]:
        """Generate alternative writing angles based on analysis."""
        angles = set()

        # Extract high-performing categories
        for p in profiles[:5]:
            if p.overall_score >= 70:
                cat = p.category
                if cat == "tech":
                    angles.add(f"{topic}: 入门到精通")
                    angles.add(f"{topic}: 打工人必备技巧")
                elif cat == "lifestyle":
                    angles.add(f"{topic}: N个亲测有效的方法")
                    angles.add(f"{topic}: 过来人的经验分享")
                elif cat == "entertainment":
                    angles.add(f"{topic}: 看完这个再也不用XXX")
                else:
                    angles.add(f"{topic}: 你不知道的N个真相")

        # Add generic angles
        angles.add(f"{topic}: N个实用技巧")
        angles.add(f"{topic}: 一篇讲清楚")

        return list(angles)[:6]

    def _detect_trend(self, profiles: list[ArticleViralProfile]) -> str:
        """Detect trend direction from current snapshot (basic heuristic)."""
        if not profiles:
            return "stable"

        high_scorers = sum(1 for p in profiles if p.overall_score >= 75)
        ratio = high_scorers / len(profiles)

        if ratio >= 0.4:
            return "rising"
        elif ratio <= 0.1:
            return "declining"
        return "stable"

    def _extract_rising_keywords(
        self, profiles: list[ArticleViralProfile]
    ) -> list[dict]:
        """Extract frequently occurring keywords as 'rising' signals."""
        # Simple frequency-based extraction (no time-series yet)
        word_freq: Counter = Counter()
        for p in profiles:
            words = self._tokenize_zh(p.title)
            for w in words:
                if len(w) >= 2:
                    word_freq[w] += 1

        return [
            {"word": word, "frequency": freq, "signal": "高频"}
            for word, freq in word_freq.most_common(30)
        ]

    @staticmethod
    def _tokenize_zh(text: str) -> list[str]:
        """Simple Chinese tokenizer (character bigrams as fallback)."""
        if not text:
            return []
        # Simple split by non-Chinese characters
        tokens = re.findall(r'[一-鿿]+', text)
        words = []
        for t in tokens:
            # 2-char sliding windows for Chinese (bigram approximation)
            if len(t) <= 4:
                words.append(t)
            else:
                for i in range(0, len(t) - 1, 2):
                    bigram = t[i:i+4]
                    if len(bigram) >= 4:
                        words.append(bigram)
                    elif len(bigram) >= 2:
                        words.append(bigram)
        return list(set(words))

    @staticmethod
    def _to_dict(obj: Any) -> dict:
        """Convert Pydantic/object to dict if needed."""
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        if isinstance(obj, dict):
            return obj
        return {}


# ── Factory ───────────────────────────────────────────────────────────────


def create_analyzer(
    config: Optional[ViralScoringConfig] = None,
) -> ViralFactorAnalyzer:
    """
    Args:
        config: Optional scoring config override. Uses defaults if None.

    Returns:
        Configured ViralFactorAnalyzer instance.
    """
    return ViralFactorAnalyzer(config=config)
