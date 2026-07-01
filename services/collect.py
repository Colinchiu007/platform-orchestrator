"""URL content collection service.

Extracted from content-aggregator v2's collector.py.
Standalone — no database, no FastAPI dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura


@dataclass
class CollectResult:
    title: str
    content: str
    author: Optional[str] = None
    word_count: int = 0
    source_url: str = ""


def _count_words(text: str) -> int:
    """Count CJK characters + English words in mixed text."""
    import re

    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return cjk + english_words


async def collect_url(url: str, timeout: int = 30) -> CollectResult:
    """Fetch and extract article content from a URL.

    Uses trafilatura for HTML → Markdown extraction.
    Raises ValueError on fetch failure.
    """
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    html = response.text
    content = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        output_format="markdown",
    )

    if not content:
        raise ValueError(f"Could not extract content from {url}")

    title = trafilatura.extract(html, output_format="markdown", include_links=False)
    if title:
        title = title.split("\n")[0].strip("# ")

    author = None
    author_meta = trafilatura.extract(
        html, output_format="markdown", with_metadata=True
    )
    if author_meta:
        import re

        m = re.search(r"author[:\s]+(.+)", author_meta, re.IGNORECASE)
        if m:
            author = m.group(1).strip()

    return CollectResult(
        title=title or url,
        content=content,
        author=author,
        word_count=_count_words(content),
        source_url=url,
    )
