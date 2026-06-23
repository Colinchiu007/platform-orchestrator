"""Tests for Story2Video text segmentation.

RED phase — write failing tests first, then implement.
"""

import re

import pytest

from services.story2video.text_segmentation import Scene, segment_text


class TestSceneDataclass:
    """Verify Scene dataclass structure."""

    def test_scene_has_expected_fields(self):
        """Scene should have text, start_time, end_time, estimated_word_count."""
        scene = Scene(text="Hello world.", start_time=0.0, end_time=1.5, estimated_word_count=2)
        assert scene.text == "Hello world."
        assert scene.start_time == 0.0
        assert scene.end_time == 1.5
        assert scene.estimated_word_count == 2

    def test_scene_immutable_text(self):
        """Scene dataclass should be frozen with safe text."""
        scene = Scene(text="test", start_time=0.0, end_time=1.0, estimated_word_count=1)
        assert isinstance(scene.text, str)


class TestSingleSentence:
    """Single sentence → exactly one scene."""

    def test_single_sentence_one_scene(self):
        """One sentence returns exactly one scene with correct timing."""
        text = "The quick brown fox jumps over the lazy dog."
        scenes = segment_text(text)
        assert len(scenes) == 1
        # 9 words / 2 wps = 4.5s
        assert scenes[0].start_time == 0.0
        assert scenes[0].end_time == 9 / 2.0
        assert scenes[0].estimated_word_count == 9
        assert "quick brown fox" in scenes[0].text


class TestMultipleScenes:
    """Multiple sentences grouped into scenes by duration."""

    def test_multi_sentence_groups_into_multiple_scenes(self):
        """Sentences should group into scenes respecting max_scene_duration."""
        # 5 sentences, each 3 words → 1.5s each
        text = "A short sentence. Another short line. Third small phrase. Fourth tiny text. Fifth little bit."
        scenes = segment_text(text, max_scene_duration=4.0)
        # S1(1.5)+S2(1.5)=3.0 ≤ 4.0, S3(1.5) makes 4.5 > 4.0 → Scene1 = S1+S2
        # S3(1.5)+S4(1.5)=3.0 ≤ 4.0, S5(1.5) makes 4.5 > 4.0 → Scene2 = S3+S4
        # S5(1.5) ≤ 4.0 → Scene3 = S5
        assert len(scenes) == 3
        # Scene 1: 6 words, 3.0s
        assert scenes[0].text == "A short sentence. Another short line."
        assert scenes[0].start_time == 0.0
        assert scenes[0].end_time == 3.0
        assert scenes[0].estimated_word_count == 6
        # Scene 2: 6 words, 3.0s
        assert scenes[1].text == "Third small phrase. Fourth tiny text."
        assert scenes[1].start_time == 3.0
        assert scenes[1].end_time == 6.0
        assert scenes[1].estimated_word_count == 6
        # Scene 3: 3 words, 1.5s
        assert scenes[2].text == "Fifth little bit."
        assert scenes[2].start_time == 6.0
        assert scenes[2].end_time == 7.5
        assert scenes[2].estimated_word_count == 3


class TestCJKText:
    """CJK text detection → Chinese mode (3 chars/sec)."""

    def test_cjk_text_uses_chinese_mode(self):
        """Text with >50% CJK should use 3 chars/sec duration."""
        text = "你好世界。今天天气很好。明天也会很好。"
        scenes = segment_text(text, max_scene_duration=3.0)
        # S1(5 chars)=1.67s, S2(7 chars)=2.33s, S3(7 chars)=2.33s
        # S1(1.67) ≤ 3.0, S2(2.33) makes 4.0 > 3.0 → Scene1 = S1
        # S2(2.33) ≤ 3.0, S3(2.33) makes 4.67 > 3.0 → Scene2 = S2
        # S3(2.33) ≤ 3.0 → Scene3 = S3
        assert len(scenes) == 3
        assert scenes[0].start_time == pytest.approx(0.0)
        assert scenes[0].end_time == pytest.approx(5.0 / 3.0, rel=1e-3)
        assert scenes[1].start_time == pytest.approx(5.0 / 3.0, rel=1e-3)
        assert scenes[1].end_time == pytest.approx(12.0 / 3.0, rel=1e-3)
        assert scenes[2].start_time == pytest.approx(12.0 / 3.0, rel=1e-3)
        assert scenes[2].end_time == pytest.approx(19.0 / 3.0, rel=1e-3)

    def test_non_cjk_text_uses_english_mode(self):
        """Text with ≤50% CJK should use 2 words/sec duration."""
        text = "Hello world. Good morning. How are you today."
        scenes = segment_text(text, max_scene_duration=10.0)
        assert len(scenes) == 1
        assert scenes[0].estimated_word_count == 8  # 2+2+4 words


class TestLongSentenceEnforcement:
    """A single long sentence exceeding max_scene_duration stays as one scene."""

    def test_long_sentence_not_split(self):
        """A single sentence exceeding max_scene_duration should be one scene."""
        # One long sentence with no sentence boundaries inside
        text = "intelligent machines continue to evolve and reshape our understanding of what technology can achieve and we must carefully consider both the tremendous opportunities and the profound challenges that lie ahead as we navigate this uncharted territory"
        word_count = len(text.split())
        scenes = segment_text(text, max_scene_duration=5.0)
        assert len(scenes) == 1
        assert scenes[0].estimated_word_count == word_count


class TestEmptyText:
    """Empty text → empty list."""

    def test_empty_text(self):
        """Empty string should return empty list."""
        scenes = segment_text("")
        assert scenes == []

    def test_whitespace_only_text(self):
        """Whitespace-only string should return empty list."""
        scenes = segment_text("   \n\n  \t  ")
        assert scenes == []


class TestMaxScenes:
    """max_scenes parameter caps scene count."""

    def test_max_scenes_cap(self):
        """Very long text should be capped at max_scenes."""
        text = ("A short sentence. " * 20).strip()
        scenes = segment_text(text, max_scene_duration=4.0, max_scenes=2)
        assert len(scenes) == 2


class TestMixedText:
    """Mixed CJK and English text."""

    def test_mixed_cjk_and_english(self):
        """Mixed text should process without errors and produce valid scenes."""
        # English-dominant text (CJK < 50%)
        text = "Hello world. 你好。How are you today. 明天见。"
        scenes = segment_text(text)
        assert len(scenes) >= 1
        for scene in scenes:
            assert isinstance(scene.text, str)
            assert len(scene.text) > 0
            assert scene.start_time >= 0.0
            assert scene.end_time > scene.start_time
            assert scene.estimated_word_count > 0


class TestWhitespaceTrim:
    """Scene text whitespace handling."""

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace should be removed from scene text."""
        text = "  Hello world.   Another sentence.  "
        scenes = segment_text(text, max_scene_duration=30.0)
        assert len(scenes) == 1
        assert not scenes[0].text.startswith(" ")
        assert not scenes[0].text.endswith(" ")
        assert "Hello world. Another sentence." == scenes[0].text

    def test_sentence_boundary_whitespace(self):
        """Internal whitespace between sentences should collapse to one space."""
        text = "First sentence.   Second   sentence."
        scenes = segment_text(text, max_scene_duration=30.0)
        assert len(scenes) == 1
        # Internal spaces within a sentence are preserved, but `   ` between sentences
        # gets consumed by the split
        assert "First sentence. Second   sentence." == scenes[0].text


class TestMultiplePunctuation:
    """Various sentence-ending punctuation marks."""

    def test_question_and_exclamation(self):
        """Question marks and exclamation points are valid sentence boundaries."""
        text = "Is this working? Yes it is! Great."
        scenes = segment_text(text, max_scene_duration=10.0)
        assert len(scenes) == 1
        assert "?" in scenes[0].text
        assert "!" in scenes[0].text

    def test_chinese_punctuation_boundaries(self):
        """Chinese 。and ！are valid sentence boundaries."""
        text = "今天天气真好。我们一起玩吧！明天见。"
        scenes = segment_text(text, max_scene_duration=30.0)
        assert len(scenes) == 1
        assert "。" in scenes[0].text
        assert "！" in scenes[0].text
