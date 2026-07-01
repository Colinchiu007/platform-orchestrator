"""Block 引擎单元测试 — 覆盖注册表 / 图验证 / 执行流程 / 异常处理。

测试策略：
- 使用纯内存 Block（不依赖任何外部服务）
- 隔离测试每个组件
- 验证状态机转换、输入解析、错误传播
"""

from __future__ import annotations

from typing import AsyncGenerator

import pydantic
import pytest

from engine.block import (
    Block,
    BlockOutput,
    clear_registry,
    get_block,
    list_blocks,
    register_block,
)
from engine.errors import (
    BlockExecutionError,
    BlockNotFoundError,
    GraphValidationError,
)
from engine.executor import ExecutionContext, ExecutionEngine, NodeStatus
from engine.graph import Graph, Link, Node


# ── 辅助：测试用 Block ───────────────────────────────────────────────────────


class TestInput(pydantic.BaseModel):
    value: str = ""


class TestOutput(pydantic.BaseModel):
    result: str = ""
    extra: dict = {}


@register_block
class EchoBlock(Block[TestInput, TestOutput]):
    """回显 Block — 将输入 value + node_id 拼成结果。"""
    id = "echo"
    name = "回显"
    description = "测试用：回显输入值"
    input_schema = TestInput
    output_schema = TestOutput

    async def run(self, inputs: TestInput) -> AsyncGenerator[BlockOutput, None]:
        yield ("result", f"echo:{inputs.value}")
        yield ("output", TestOutput(result=f"echo:{inputs.value}").model_dump())


@register_block
class FailBlock(Block[TestInput, TestOutput]):
    """总是失败的 Block — 测试错误处理。"""
    id = "fail"
    name = "总是失败"
    description = "测试用：总是抛出异常"
    input_schema = TestInput
    output_schema = TestOutput

    async def run(self, inputs: TestInput) -> AsyncGenerator[BlockOutput, None]:
        raise BlockExecutionError(block_id="fail", reason="故意失败")
        yield  # pragma: no cover


@register_block
class ProgressBlock(Block[TestInput, TestOutput]):
    """带进度的 Block — 测试流式输出。"""
    id = "progress"
    name = "进度输出"
    description = "测试用：流式输出进度消息"
    input_schema = TestInput
    output_schema = TestOutput

    async def run(self, inputs: TestInput) -> AsyncGenerator[BlockOutput, None]:
        yield ("progress", '{"pct": 0}')
        yield ("progress", '{"pct": 50}')
        yield ("result", "done")
        yield ("output", TestOutput(result="done").model_dump())


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前清空注册表，后注册测试 Block。"""
    clear_registry()
    register_block(EchoBlock)
    register_block(FailBlock)
    register_block(ProgressBlock)
    yield


# ══════════════════════════════════════════════════════════════════════════════
# Block 注册表
# ══════════════════════════════════════════════════════════════════════════════


class TestBlockRegistry:
    def test_register_and_get(self):
        cls = get_block("echo")
        assert cls is EchoBlock
        assert cls.id == "echo"
        assert cls.name == "回显"

    def test_get_unknown_block(self):
        with pytest.raises(ValueError, match="未注册"):
            get_block("nonexistent")

    def test_list_blocks(self):
        infos = list_blocks()
        ids = [i["id"] for i in infos]
        assert "echo" in ids
        assert "fail" in ids
        assert "progress" in ids

    def test_register_non_block_raises(self):
        with pytest.raises(TypeError, match="不是 Block"):
            register_block(int)  # type: ignore

    def test_block_to_dict(self):
        cls = get_block("echo")
        info = cls().to_dict()
        assert info["id"] == "echo"
        assert "input_schema" in info
        assert "output_schema" in info
        assert info["input_schema"]["title"] == "TestInput"


# ══════════════════════════════════════════════════════════════════════════════
# Graph 模型
# ══════════════════════════════════════════════════════════════════════════════


class TestGraph:
    def test_empty_graph(self):
        g = Graph(id="test", nodes=[], links=[])
        errors = g.validate()
        assert "图中没有节点" in errors

    def test_duplicate_node_ids(self):
        with pytest.raises(ValueError, match="重复"):
            Graph(
                nodes=[
                    Node(id="a", block_id="echo"),
                    Node(id="a", block_id="echo"),
                ]
            )

    def test_duplicate_links(self):
        with pytest.raises(ValueError, match="重复"):
            Graph(
                nodes=[
                    Node(id="a", block_id="echo"),
                    Node(id="b", block_id="echo"),
                ],
                links=[
                    Link(source_id="a", source_output="x", target_id="b", target_input="y"),
                    Link(source_id="a", source_output="x", target_id="b", target_input="y"),
                ],
            )

    def test_dangling_link(self):
        g = Graph(
            nodes=[Node(id="a", block_id="echo")],
            links=[Link(source_id="a", source_output="x", target_id="ghost", target_input="y")],
        )
        errors = g.validate()
        assert any("ghost" in e for e in errors)

    def test_cycle_detection(self):
        """a → b → a"""
        g = Graph(
            nodes=[
                Node(id="a", block_id="echo"),
                Node(id="b", block_id="echo"),
            ],
            links=[
                Link(source_id="a", source_output="x", target_id="b", target_input="y"),
                Link(source_id="b", source_output="x", target_id="a", target_input="y"),
            ],
        )
        with pytest.raises(GraphValidationError, match="环"):
            g.topological_sort()

    def test_topological_sort_linear(self):
        """a → b → c"""
        g = Graph(
            nodes=[
                Node(id="a", block_id="echo"),
                Node(id="b", block_id="echo"),
                Node(id="c", block_id="echo"),
            ],
            links=[
                Link(source_id="a", source_output="x", target_id="b", target_input="y"),
                Link(source_id="b", source_output="x", target_id="c", target_input="y"),
            ],
        )
        order = g.topological_sort()
        assert order == ["a", "b", "c"]

    def test_topological_sort_independent(self):
        """a, b 独立 — 可并行"""
        g = Graph(
            nodes=[
                Node(id="a", block_id="echo"),
                Node(id="b", block_id="echo"),
            ],
            links=[],
        )
        order = g.topological_sort()
        assert set(order) == {"a", "b"}

    def test_validate_healthy_graph(self):
        g = Graph(
            id="healthy",
            nodes=[
                Node(id="s1", block_id="echo"),
                Node(id="s2", block_id="echo"),
            ],
            links=[
                Link(source_id="s1", source_output="x", target_id="s2", target_input="y"),
            ],
        )
        assert g.validate() == []


# ══════════════════════════════════════════════════════════════════════════════
# 执行引擎
# ══════════════════════════════════════════════════════════════════════════════


class TestExecutionEngine:
    @pytest.mark.asyncio
    async def test_single_block_execution(self):
        g = Graph(
            id="single",
            nodes=[Node(id="n1", block_id="echo", input_data={"value": "hello"})],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)

        assert result.success is True
        assert result.node_states["n1"] == NodeStatus.COMPLETED
        assert result.get_output("n1", "result") == "echo:hello"

    @pytest.mark.asyncio
    async def test_linear_pipeline(self):
        """a → b 线性 Flow，b 消费 a 的输出。"""
        g = Graph(
            id="linear",
            nodes=[
                Node(id="a", block_id="echo", input_data={"value": "hello"}),
                Node(id="b", block_id="echo"),
            ],
            links=[
                Link(source_id="a", source_output="result", target_id="b", target_input="value"),
            ],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)

        assert result.success is True
        assert result.get_output("a", "result") == "echo:hello"
        assert result.get_output("b", "result") == "echo:echo:hello"

    @pytest.mark.asyncio
    async def test_block_failure_propagates(self):
        """失败 Block 中止图执行。"""
        g = Graph(
            id="fail_test",
            nodes=[
                Node(id="good", block_id="echo", input_data={"value": "ok"}),
                Node(id="bad", block_id="fail"),
            ],
            links=[
                Link(source_id="good", source_output="result", target_id="bad", target_input="value"),
            ],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)

        assert result.success is False
        assert result.node_states["good"] == NodeStatus.COMPLETED
        assert result.node_states["bad"] == NodeStatus.FAILED
        assert "故意失败" in result.node_errors["bad"]

    @pytest.mark.asyncio
    async def test_progress_streaming(self):
        """Block 可以流式输出多条消息。"""
        g = Graph(
            id="progress_test",
            nodes=[Node(id="p1", block_id="progress")],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)

        assert result.success is True
        assert result.get_output("p1", "result") == "done"

    @pytest.mark.asyncio
    async def test_input_precedence(self):
        """input_data 优先级高于 Link 输入。"""
        g = Graph(
            id="precedence",
            nodes=[
                Node(id="src", block_id="echo", input_data={"value": "from_const"}),
                Node(id="dst", block_id="echo", input_data={"value": "override"}),
            ],
            links=[
                Link(source_id="src", source_output="result", target_id="dst", target_input="value"),
            ],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)
        assert result.get_output("src", "result") == "echo:from_const"
        assert result.get_output("dst", "result") == "echo:override"

    @pytest.mark.asyncio
    async def test_graph_fails_validation(self):
        """不合法的图不执行，直接抛异常。"""
        g = Graph(
            id="invalid",
            nodes=[Node(id="a", block_id="echo")],
            links=[Link(source_id="a", source_output="x", target_id="b", target_input="y")],
        )
        engine = ExecutionEngine()
        with pytest.raises(GraphValidationError, match="不存在"):
            await engine.run(g)

    @pytest.mark.asyncio
    async def test_execution_context_passed(self):
        """ExecutionContext 的 config 作为全局 fallback。"""
        g = Graph(
            id="ctx_test",
            nodes=[Node(id="n1", block_id="echo")],
        )
        ctx = ExecutionContext(
            graph=g,
            job_id="job-001",
            config={"value": "from_ctx"},
        )
        engine = ExecutionEngine()
        result = await engine.run(g, ctx)

        assert result.success is True
        assert result.get_output("n1", "result") == "echo:from_ctx"

    @pytest.mark.asyncio
    async def test_failed_nodes_property(self):
        g = Graph(
            id="fail_prop",
            nodes=[Node(id="bad", block_id="fail")],
        )
        engine = ExecutionEngine()
        result = await engine.run(g)
        assert result.failed_nodes == ["bad"]
