"""Story2Video text segmentation module.

Splits text into timed scenes based on sentence boundaries and duration limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Scene:
    """A scene segment with timing information."""

    text: str
    start_time: float
    end_time: float
    estimated_word_count: int


# Regex to split text into sentences at boundaries.
# Two alternatives:
# 1. Western punctuation (. ! ?) followed by whitespace — consumes the whitespace
# 2. CJK punctuation (。 ！) — zero-width split (no whitespace between CJK sentences)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。！])")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_chinese_mode(text: str) -> bool:
    """Detect if text is primarily Chinese (>50% CJK characters of non-space chars)."""
    cjk_count = len(_CJK_RE.findall(text))
    non_space_count = sum(1 for c in text if not c.isspace())
    if non_space_count == 0:
        return False
    return cjk_count / non_space_count > 0.5


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences at recognized boundaries, stripping each."""
    parts = _SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def segment_text(
    text: str,
    max_scene_duration: float = 30.0,
    max_scenes: int | None = None,
) -> list[Scene]:
    """Split text into timed scenes based on sentence boundaries.

    Args:
        text: Input text to segment.
        max_scene_duration: Maximum duration per scene in seconds.
        max_scenes: Optional maximum number of scenes to return.

    Returns:
        List of Scene objects with timing information.
    """
    if not text or not text.strip():
        return []

    is_cjk = _is_chinese_mode(text)
    sentences = _split_sentences(text)

    if not sentences:
        return []

    scenes: list[Scene] = []
    current_sentences: list[str] = []
    current_duration = 0.0
    current_word_count = 0
    current_time = 0.0

    for sentence in sentences:
        if is_cjk:
            duration = len(sentence) / 3.0
            word_count = len(sentence)
        else:
            words = len(sentence.split())
            duration = words / 2.0
            word_count = words

        # If this sentence would exceed max_scene_duration and we already have
        # content in the current scene, flush the current scene first.
        if current_sentences and current_duration + duration > max_scene_duration:
            scenes.append(
                Scene(
                    text=" ".join(current_sentences),
                    start_time=current_time,
                    end_time=current_time + current_duration,
                    estimated_word_count=current_word_count,
                )
            )
            current_time += current_duration
            current_sentences = []
            current_duration = 0.0
            current_word_count = 0

        current_sentences.append(sentence)
        current_duration += duration
        current_word_count += word_count

    # Flush the remaining (or only) scene.
    if current_sentences:
        scenes.append(
            Scene(
                text=" ".join(current_sentences),
                start_time=current_time,
                end_time=current_time + current_duration,
                estimated_word_count=current_word_count,
            )
        )

    # Apply max_scenes cap.
    if max_scenes is not None:
        scenes = scenes[:max_scenes]

    return scenes
