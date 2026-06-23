"""Service modules for platform-orchestrator.

Phase 1 (content pipeline):
- collect.py: URL content collection (trafilatura)
- rewrite.py: LLM article rewriting (4 styles × 3 lengths)

Phase 2 (Story2Video):
- tts_service.py: Doubao TTS + voice cloning
- prompt_service.py: Scene-to-prompt optimization (LLM)
- image_service.py: Multi-provider image generation
- video_service.py: Multi-provider video generation
- compositor.py: FFmpeg video compositing (Canvas replacement)
"""

from services.collect import CollectResult, collect_url
from services.compositor import (
    CompositorInput,
    CompositorResult,
    SubtitleSegment,
    compose_from_pipeline,
    compose_video,
)
from services.image_service import (
    GenerateImageRequest,
    ImageProvider,
    ImageResult,
    ImageStatus,
    generate_image,
    generate_images_batch,
)
from services.prompt_service import OptimizePromptResult, optimize_prompt, optimize_prompts_batch
from services.rewrite import LENGTH_INSTRUCTIONS, STYLE_PROMPTS, RewriteResult, rewrite_content
from services.tts_service import TTSResult, VoiceCloneResult, clone_voice, text_to_speech
from services.video_service import (
    GenerateVideoRequest,
    VideoProvider,
    VideoResult,
    VideoStatus,
    generate_video,
    query_video_status,
)

__all__ = [
    # Collect
    "collect_url", "CollectResult",
    # Rewrite
    "rewrite_content", "RewriteResult", "STYLE_PROMPTS", "LENGTH_INSTRUCTIONS",
    # TTS
    "text_to_speech", "clone_voice", "TTSResult", "VoiceCloneResult",
    # Prompt
    "optimize_prompt", "optimize_prompts_batch", "OptimizePromptResult",
    # Image
    "generate_image", "generate_images_batch", "GenerateImageRequest",
    "ImageProvider", "ImageResult", "ImageStatus",
    # Video
    "generate_video", "query_video_status", "GenerateVideoRequest",
    "VideoProvider", "VideoResult", "VideoStatus",
    # Compositor
    "compose_video", "compose_from_pipeline", "CompositorInput",
    "SubtitleSegment", "CompositorResult",
]
