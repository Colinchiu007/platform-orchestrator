"""Pipeline v2 — Block Graph based pipeline execution.

Builds a 5-step DAG using registered Blocks and runs via ExecutionEngine.
Replaces the hardcoded step functions in pipeline.py.

DAG structure::

    splitter ──scenes──→ optimizer ──prompts──→ image_gen ──image_paths─┐
        │                                                                │
        └──scenes──→ tts ──────────audio_path─────────────────────────→ compose

Each node's ``status`` is written to ``jobs`` table as it runs,
preserving the same step labels ("splitting", "optimizing", "tts",
"imaging", "composing") that the frontend polls.
"""

from __future__ import annotations

import json
import logging
import os

import aiosqlite

from engine.executor import (
    CallbackConfig,
    ExecutionContext,
    ExecutionEngine,
    ExecutionResult,
    RetryPolicy,
)
from engine.graph import Graph, Node, Link

# 触发所有 Block 注册
import blocks  # noqa: F401

logger = logging.getLogger(__name__)

# ── 状态映射 ──────────────────────────────────────────────────────────────────

_STEP_STATUS: dict[str, str] = {
    "splitter": "splitting",
    "optimizer": "optimizing",
    "tts": "tts",
    "image_gen": "imaging",
    "compose": "composing",
}


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _scratch_dir(job_id: str) -> str:
    d = os.path.join("/tmp", "pipeline", job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _ratio_dims(ratio: str) -> tuple[int, int]:
    return {
        "9:16": (720, 1280),
        "16:9": (1280, 720),
        "1:1": (720, 720),
    }.get(ratio, (720, 1280))


# ── 图构造器 ──────────────────────────────────────────────────────────────────


def build_pipeline_graph(
    content: str,
    voice: str,
    prompt_platform: str,
    video_ratio: str,
    job_id: str,
) -> Graph:
    """构建 5 步管线 DAG。

    Args:
        content: 原始文本内容
        voice: 音色 ID
        prompt_platform: 提示词平台（midjourney/sd_xl/dall_e 等）
        video_ratio: 视频比例（9:16/16:9/1:1）
        job_id: 任务 ID（用于生成临时目录和输出路径）

    Returns:
        可执行的 Graph 对象
    """
    scratch = _scratch_dir(job_id)
    width, height = _ratio_dims(video_ratio)
    output_path = os.path.join(scratch, f"{job_id}.mp4")

    nodes = [
        Node(
            id="splitter",
            block_id="splitter",
            input_data={"content": content},
        ),
        Node(
            id="optimizer",
            block_id="optimizer",
            config={"platform": prompt_platform},
        ),
        Node(
            id="tts",
            block_id="tts",
            config={"voice": voice},
            input_data={"output_dir": os.path.join(scratch, "audio")},
        ),
        Node(
            id="image_gen",
            block_id="image_gen",
            input_data={"output_dir": os.path.join(scratch, "images")},
        ),
        Node(
            id="compose",
            block_id="compose",
            config={
                "width": width,
                "height": height,
                "image_duration": 6.0,
                "fps": 30,
            },
            input_data={"output_path": output_path},
        ),
    ]

    links = [
        Link(
            source_id="splitter", source_output="scenes",
            target_id="optimizer", target_input="scenes",
        ),
        Link(
            source_id="splitter", source_output="scenes",
            target_id="tts", target_input="scenes",
        ),
        Link(
            source_id="optimizer", source_output="prompts",
            target_id="image_gen", target_input="prompts",
        ),
        Link(
            source_id="tts", source_output="audio_path",
            target_id="compose", target_input="audio_path",
        ),
        Link(
            source_id="image_gen", source_output="image_paths",
            target_id="compose", target_input="image_paths",
        ),
    ]

    return Graph(
        id=f"pipeline-{job_id}",
        description="Pipeline v2 — 5-step video generation",
        nodes=nodes,
        links=links,
    )


# ── 运行入口 ──────────────────────────────────────────────────────────────────


async def run_block_pipeline(
    db_path: str,
    job_id: str,
    content: str,
    *,
    voice: str = "zh-CN-XiaoxiaoNeural",
    prompt_platform: str = "midjourney",
    video_ratio: str = "9:16",
) -> ExecutionResult:
    """运行 Block Graph 管线。

    这是 ``pipeline.run_pipeline()`` 的 v2 实现。
    提供相同的语义但通过 Block Graph 引擎执行。

    Args:
        db_path: SQLite 数据库路径（用于状态持久化）
        job_id: 任务 ID
        content: 输入文本
        voice: 音色 ID
        prompt_platform: 提示词平台
        video_ratio: 视频比例

    Returns:
        执行结果（含各节点状态和输出）
    """
    scratch = _scratch_dir(job_id)

    # 1. 构建图
    graph = build_pipeline_graph(
        content=content,
        voice=voice,
        prompt_platform=prompt_platform,
        video_ratio=video_ratio,
        job_id=job_id,
    )

    # 2. 创建执行上下文
    context = ExecutionContext(
        graph=graph,
        job_id=job_id,
        db_path=db_path,
        scratch_dir=scratch,
    )

    # 3. 重试策略
    retry_policy = RetryPolicy(max_retries=2)

    # 4. 注册每节点状态回调 → 更新 jobs.status
    _stopped = False

    async def _update_status(node_id: str) -> None:
        """节点开始时更新 jobs.status。"""
        nonlocal _stopped
        if _stopped:
            return
        step = _STEP_STATUS.get(node_id)
        if not step:
            return
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (step, job_id),
            )
            await db.commit()

    # 注入到 context.config，让 engine 在每节点执行前调用
    context.on_node_start = _update_status

    # 5. 注册完成回调 → 更新最终状态
    async def _finalize(result: ExecutionResult) -> None:
        nonlocal _stopped
        async with aiosqlite.connect(db_path) as db:
            if result.success:
                output_path = result.get_output("compose", "output_path", "")
                await db.execute(
                    "UPDATE jobs SET status = 'done', output_path = ? WHERE id = ?",
                    (output_path, job_id),
                )
            else:
                errors = "; ".join(
                    f"{nid}: {err}"
                    for nid, err in result.node_errors.items()
                )
                await db.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (errors[:500], job_id),
                )
            await db.commit()
        logger.info("Job %s: final status=%s", job_id, "done" if result.success else "failed")
        _stopped = True

    context.callbacks = CallbackConfig(
        on_complete=_finalize,
        on_fail=_finalize,
    )

    # 6. 执行
    engine = ExecutionEngine()
    result = await engine.run(graph, context, retry_policy)
    return result
