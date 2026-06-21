"""Router stubs for the platform-orchestrator.

Each module gets its own router file. In Phase 0 these are stubs.
Module integration happens in Phases 1-3.
"""

from routers import aggregator, prompt, publish, splitter, video

__all__ = ["aggregator", "splitter", "prompt", "video", "publish"]
