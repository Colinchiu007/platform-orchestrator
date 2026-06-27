"""Block-based video pipeline (v2) — replaces the hardcoded 4-step flow.

Builds a Graph from registered Blocks and executes via ExecutionEngine.
New code path gated by ``pipeline_v2`` feature gate. Old path retained.

Usage:
    result = await run_pipeline_v2(
        job_id="xxx",
        article_id="xxx",
        split_json={"scenes": [...]},
        image_effect="zoom-in",
        transition="fade",
        voice_id="zh-CN-XiaoxiaoNeural",
        image_provider="minimax",
    )
"""

from __future__ import annotations

import json
import logging
import os

import aiosqlite

from engine.executor import ExecutionContext, ExecutionEngine
from engine.graph import Graph, Node, Link

logger = logging.getLogger(__name__)

DB_PATH = "orchestrator.db"

# ── 管线常量 ──────────────────────────────────────────────────────────────────


def _scratch_dir(job_id: str) -> str:
    d = os.path.join("/tmp", "pipeline", job_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── 管线定义 ──────────────────────────────────────────────────────────────────


def build_pipeline_graph(
    scenes: list[dict],
    scratch: str,
    voice: str,
    image_provider: str,
    image_effect: str,
    transition: str,
) -> Graph:
    """构建标准的 4 步视频管线 Graph。

    split 已经在路由层完成（从 DB 读 split_json），这里跑：
    TTS → prompt optimize → image gen → compose
    """
    return Graph(
        id="video-pipeline-v2",
        description="标准视频生成管线 (Block 引擎)",
        nodes=[
            Node(
                id="tts",
                block_id="tts",
                input_data={
                    "scenes": scenes,
                    "voice": voice,
                    "output_dir": scratch,
                },
            ),
            Node(
                id="optimizer",
                block_id="optimizer",
                input_data={
                    "scenes": scenes,
                    "platform": image_provider,
                },
            ),
            Node(
                id="image_gen",
                block_id="image_gen",
                config={"output_dir": scratch},
                # prompts 来自 optimizer 的输出
            ),
            Node(
                id="compose",
                block_id="compose",
                config={
                    "width": 1280,
                    "height": 720,
                    "image_duration": 6.0,
                    "output_path": os.path.join(scratch, "final.mp4"),
                },
                # image_paths 来自 image_gen, audio_path 来自 tts
            ),
        ],
        links=[
            Link(
                source_id="optimizer",
                source_output="prompts",
                target_id="image_gen",
                target_input="prompts",
            ),
            Link(
                source_id="tts",
                source_output="audio_path",
                target_id="compose",
                target_input="audio_path",
            ),
            Link(
                source_id="image_gen",
                source_output="image_paths",
                target_id="compose",
                target_input="image_paths",
            ),
        ],
    )


# ── DB 工具 ───────────────────────────────────────────────────────────────────


async def _update_job(job_id: str, status: str, **extra) -> None:
    """更新任务状态到 DB。"""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    try:
        output_data = extra.get("output_data")
        error = extra.get("error")
        sql = (
            "UPDATE jobs SET status = ?, output_data = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ?"
        )
        await db.execute(sql, (
            status,
            json.dumps(output_data or {}),
            error,
            job_id,
        ))
        await db.commit()
    finally:
        await db.close()


# ── 公开接口 ───────────────────────────────────────────────────────────────────


async def run_pipeline_v2(
    job_id: str,
    article_id: str,
    split_json: dict,
    image_effect: str = "zoom-in",
    transition: str = "fade",
    voice_id: str = "zh-CN-XiaoxiaoNeural",
    image_provider: str = "minimax",
) -> dict:
    """执行 Block 引擎版视频管线。

    Args:
        job_id: 任务 ID（对应 jobs 表主键）
        article_id: 文章 ID
        split_json: 分句结果（包含 scenes 列表）
        image_effect: 图片动效
        transition: 转场效果
        voice_id: TTS 音色
        image_provider: 图片生成服务商

    Returns:
        管线执行结果字典，格式与旧版 _update("done", ...) 一致

    Raises:
        RuntimeError: 管线执行失败
    """
    scenes = split_json.get("scenes", [])
    if not scenes:
        raise ValueError("split_json 中没有 scenes")

    scratch = _scratch_dir(job_id)

    # 1. 构建 Graph
    graph = build_pipeline_graph(
        scenes=scenes,
        scratch=scratch,
        voice=voice_id,
        image_provider=image_provider,
        image_effect=image_effect,
        transition=transition,
    )

    # 2. 创建执行上下文
    ctx = ExecutionContext(
        graph=graph,
        job_id=job_id,
        db_path=DB_PATH,
        scratch_dir=scratch,
    )

    # 3. 执行
    engine = ExecutionEngine()

    await _update_job(job_id, "processing", output_data={"progress": 0.0})

    try:
        result = await engine.run(graph, ctx)
    except Exception as e:
        await _update_job(job_id, "failed", error=str(e))
        raise

    # 4. 检查执行结果
    if not result.success:
        failed = result.failed_nodes
        errors = {nid: result.node_errors.get(nid, "unknown") for nid in failed}
        error_msg = f"管线步骤失败: {errors}"
        await _update_job(job_id, "failed", error=error_msg)
        raise RuntimeError(error_msg)

    # 5. 提取产出
    audio_path = result.get_output("tts", "audio_path", "")
    image_paths = result.get_output("image_gen", "image_paths", [])
    compose_output = result.get_output("compose", "output", {})
    output_path = compose_output.get("output_path", "")
    duration = compose_output.get("duration_seconds", 0.0)

    output = {
        "progress": 1.0,
        "output_path": output_path,
        "duration": duration,
        "scenes": len(scenes),
        "images_generated": len(image_paths),
        "pipeline_version": "v2",
    }

    await _update_job(job_id, "done", output_data=output)

    logger.info(
        "Pipeline v2 done: job=%s scenes=%d images=%d duration=%.1fs",
        job_id, len(scenes), len(image_paths), duration,
    )

    return output