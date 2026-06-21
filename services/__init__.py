"""Service modules for platform-orchestrator.

- collect.py: URL content collection (trafilatura-based)
- rewrite.py: LLM article rewriting (OpenAI-compatible API)
"""

from services.collect import collect_url, CollectResult
from services.rewrite import rewrite_content, RewriteResult, STYLE_PROMPTS, LENGTH_INSTRUCTIONS

__all__ = [
    "collect_url",
    "CollectResult",
    "rewrite_content",
    "RewriteResult",
    "STYLE_PROMPTS",
    "LENGTH_INSTRUCTIONS",
]
