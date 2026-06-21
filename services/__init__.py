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

from services.collect import collect_url, CollectResult
from services.rewrite import rewrite_content, RewriteResult, STYLE_PROMPTS, LENGTH_INSTRUCTIONS
from services.tts_service import text_to_speech, clone_voice, TTSResult, VoiceCloneResult
from services.prompt_service import optimize_prompt, optimize_prompts_batch, OptimizePromptResult
from services.image_service import generate_image, generate_images_batch, GenerateImageRequest, ImageProvider, ImageResult, ImageStatus
from services.video_service import generate_video, query_video_status, GenerateVideoRequest, VideoProvider, VideoResult, VideoStatus
from services.compositor import compose_video, compose_from_pipeline, CompositorInput, SubtitleSegment, CompositorResult

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
