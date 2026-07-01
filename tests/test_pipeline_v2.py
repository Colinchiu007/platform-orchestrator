"""pipeline_v2 集成测试 — 模拟完整管线执行，无需外部服务。

测试策略：
- 用 MockBlock 替代真实的外部服务调用（splitter/SDK/etc）
- 验证 Graph 构建、输入输出传递、DB 状态更新
- 使用临时 aiosqlite 模拟 jobs 表
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import AsyncGenerator

import aiosqlite
import pydantic
import pytest

from engine.block import Block, BlockOutput, clear_registry, register_block
from engine.executor import ExecutionContext, ExecutionEngine
from engine.graph import Graph, Node, Link
from services.pipeline_v2 import build_pipeline_graph, _scratch_dir


# ── Mock Blocks ───────────────────────────────────────────────────────────────


class MockTTSInput(pydantic.BaseModel):
    scenes: list[dict] = []
    voice: str = "test"
    output_dir: str = "/tmp"


class MockTTSOutput(pydantic.BaseModel):
    audio_path: str = ""
    duration_seconds: float = 0.0


@register_block
class MockTTSBlock(Block[MockTTSInput, MockTTSOutput]):
    id = "tts"
    name = "Mock TTS"
    input_schema = MockTTSInput
    output_schema = MockTTSOutput

    async def run(self, inputs) -> AsyncGenerator[BlockOutput, None]:
        audio_path = os.path.join(inputs.output_dir, "audio.mp3")
        open(audio_path, "w").close()  # touch file
        yield ("audio_path", audio_path)
        yield ("output", MockTTSOutput(audio_path=audio_path, duration_seconds=3.0).model_dump())


class MockOptInput(pydantic.BaseModel):
    scenes: list[dict] = []
    platform: str = "midjourney"


class MockOptOutput(pydantic.BaseModel):
    prompts: list[str] = []
    total_prompts: int = 0


@register_block
class MockOptBlock(Block[MockOptInput, MockOptOutput]):
    id = "optimizer"
    name = "Mock Optimizer"
    input_schema = MockOptInput
    output_schema = MockOptOutput

    async def run(self, inputs) -> AsyncGenerator[BlockOutput, None]:
        prompts = [f"prompt_for_scene_{i}" for i in range(len(inputs.scenes))]
        yield ("prompts", prompts)
        yield ("output", MockOptOutput(prompts=prompts, total_prompts=len(prompts)).model_dump())


class MockImgInput(pydantic.BaseModel):
    prompts: list[str] = []
    output_dir: str = "/tmp"


class MockImgOutput(pydantic.BaseModel):
    image_paths: list[str] = []


@register_block
class MockImgBlock(Block[MockImgInput, MockImgOutput]):
    id = "image_gen"
    name = "Mock ImageGen"
    input_schema = MockImgInput
    output_schema = MockImgOutput

    async def run(self, inputs) -> AsyncGenerator[BlockOutput, None]:
        paths = []
        for i, _ in enumerate(inputs.prompts):
            path = os.path.join(inputs.output_dir, f"scene_{i:03d}.png")
            open(path, "w").close()
            paths.append(path)
        yield ("image_paths", paths)
        yield ("output", MockImgOutput(image_paths=paths).model_dump())


class MockCompInput(pydantic.BaseModel):
    image_paths: list[str] = []
    audio_path: str = ""
    output_path: str = ""
    width: int = 1280
    height: int = 720
    image_duration: float = 6.0
    fps: int = 30


class MockCompOutput(pydantic.BaseModel):
    output_path: str = ""
    duration_seconds: float = 0.0


@register_block
class MockCompBlock(Block[MockCompInput, MockCompOutput]):
    id = "compose"
    name = "Mock Compose"
    input_schema = MockCompInput
    output_schema = MockCompOutput

    async def run(self, inputs) -> AsyncGenerator[BlockOutput, None]:
        output_path = inputs.output_path or "/tmp/final.mp4"
        open(output_path, "w").close()
        yield ("output_path", output_path)
        yield ("output", MockCompOutput(output_path=output_path, duration_seconds=10.0).model_dump())


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    register_block(MockTTSBlock)
    register_block(MockOptBlock)
    register_block(MockImgBlock)
    register_block(MockCompBlock)
    yield




# ══════════════════════════════════════════════════════════════════════════════
# 管线集成测试
# ══════════════════════════════════════════════════════════════════════════════


class TestPipelineV2:
    @pytest.mark.asyncio
    async def test_build_graph(self):
        """build_pipeline_graph 返回合法的 Graph。"""
        scenes = [{"text": "hello", "segment_id": 0}]
        scratch = _scratch_dir("test-graph")

        graph = build_pipeline_graph(
            scenes=scenes,
            scratch=scratch,
            voice="test",
            image_provider="midjourney",
            image_effect="zoom-in",
            transition="fade",
        )

        assert graph.validate() == []  # 图合法
        assert len(graph.nodes) == 4
        assert len(graph.links) == 3

        order = graph.topological_sort()
        # TTS 和 optimizer 谁先无所谓，它们并行；image_gen 必须等 optimizer；compose 必须最后
        assert order.index("image_gen") > order.index("optimizer")
        assert order.index("compose") > order.index("image_gen")
        assert order.index("compose") > order.index("tts")

    @pytest.mark.asyncio
    async def test_full_pipeline_execution(self):
        """完整管线执行：build → run → 产出。"""
        scenes = [
            {"text": "scene1", "segment_id": 0},
            {"text": "scene2", "segment_id": 1},
        ]
        scratch = _scratch_dir("test-full")

        graph = build_pipeline_graph(
            scenes=scenes,
            scratch=scratch,
            voice="test",
            image_provider="midjourney",
            image_effect="zoom-in",
            transition="fade",
        )

        ctx = ExecutionContext(graph=graph, job_id="test-full", scratch_dir=scratch)
        engine = ExecutionEngine()
        result = await engine.run(graph, ctx)

        assert result.success, f"Pipeline failed: {result.node_errors}"
        assert result.get_output("tts", "audio_path", "").endswith(".mp3")
        assert len(result.get_output("image_gen", "image_paths", [])) == 2
        assert result.get_output("compose", "output_path", "").endswith(".mp4")
        assert result.get_output("optimizer", "output", {}).get("total_prompts") == 2

    @pytest.mark.asyncio
    async def test_empty_scenes_fails(self):
        """空 scenes 抛 ValueError。"""
        from services.pipeline_v2 import run_pipeline_v2

        with pytest.raises(ValueError, match="没有 scenes"):
            await run_pipeline_v2(
                job_id="test-empty",
                article_id="art-1",
                split_json={"scenes": []},
            )

    @pytest.mark.asyncio
    async def test_pipeline_v2_db_writes(self):
        """run_pipeline_v2 执行后 DB 更新为 done。"""
        from services.pipeline_v2 import run_pipeline_v2

        # 覆盖 DB_PATH 为临时库
        import services.pipeline_v2 as pv2
        orig_db = pv2.DB_PATH
        # 使用临时文件代替 :memory: 保证连接共享
        import tempfile as _tf
        db_path = os.path.join(_tf.mkdtemp(), "test.db")
        pv2.DB_PATH = db_path

        db = await aiosqlite.connect(db_path)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, user_id TEXT, job_type TEXT DEFAULT 'video',
                status TEXT DEFAULT 'pending', input_data TEXT DEFAULT '{}',
                output_data TEXT DEFAULT '{}', error TEXT,
                created_at TEXT, updated_at TEXT
            )
        """)
        await db.execute(
            "INSERT INTO jobs (id, user_id, job_type, status) VALUES (?, ?, ?, ?)",
            ("test-db-job", "u1", "video", "pending"),
        )
        await db.commit()
        await db.close()

        try:
            out = await run_pipeline_v2(
                job_id="test-db-job",
                article_id="art-1",
                split_json={
                    "scenes": [{"text": "test", "segment_id": 0}]
                },
            )
            assert out["progress"] == 1.0
            assert out["pipeline_version"] == "v2"
            assert out["scenes"] == 1

            # 验证 DB 写入
            db2 = await aiosqlite.connect(db_path)
            db2.row_factory = aiosqlite.Row
            await db2.execute("PRAGMA journal_mode=WAL;")
            async with db2.execute(
                "SELECT status, output_data FROM jobs WHERE id = ?",
                ("test-db-job",),
            ) as cursor:
                row = await cursor.fetchone()
            await db2.close()
            assert row["status"] == "done"
            output = json.loads(row["output_data"])
            assert output["progress"] == 1.0
            assert output["pipeline_version"] == "v2"
        finally:
            pv2.DB_PATH = orig_db
            if os.path.exists(db_path):
                os.remove(db_path)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="video.py 依赖 FastAPI 完整启动环境，路由层不宜在单元测试中独立 import")
    async def test_video_router_imports_cleanly(self):
        """video.py 导入不报错 + pipeline 导入不报错 + pipeline_v2 分支代码可达。"""
        from routers.video import _run_video_pipeline
        assert _run_video_pipeline is not None
