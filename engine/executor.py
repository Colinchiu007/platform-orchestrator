"""DAG 执行引擎 — 调度并执行 Block Graph。

核心职责：
1. 拓扑排序确定执行顺序
2. 解析每个 Node 的输入（Link 上游 + 常量 input_data）
3. 运行 Node 对应 Block 的 run() 方法
4. 管理状态机（PENDING → READY → RUNNING → COMPLETED/FAILED）
5. 收集输出，传递给下游 Node
6. 异常处理：单节点失败可配置降级策略

不依赖 FastAPI / Celery / Redis，纯 asyncio 实现。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator

from engine.block import Block, get_block
from engine.errors import BlockExecutionError, GraphValidationError
from engine.graph import Graph, Link, Node

logger = logging.getLogger(__name__)


# ── 状态枚举 ──────────────────────────────────────────────────────────────────


class NodeStatus(str, Enum):
    PENDING = "pending"       # 初始状态，尚未就绪
    READY = "ready"           # 所有依赖满足，等待执行
    RUNNING = "running"       # 正在执行 run()
    COMPLETED = "completed"   # 执行成功
    FAILED = "failed"         # 执行失败


# ── 执行上下文 ────────────────────────────────────────────────────────────────


@dataclass
class ExecutionContext:
    """图执行上下文 — 任务级别共享数据。"""

    graph: Graph
    job_id: str = ""                     # 任务关联 ID
    db_path: str = ""                    # SQLite 路径（用于状态更新）
    scratch_dir: str = ""                # 临时文件目录
    config: dict[str, Any] = field(default_factory=dict)   # 全局配置
    shared_data: dict[str, Any] = field(default_factory=dict)  # 节点间数据缓存

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


# ── 执行引擎 ──────────────────────────────────────────────────────────────────


class ExecutionEngine:
    """Block Graph 执行引擎。

    用法:
        engine = ExecutionEngine()
        result = await engine.run(graph, context)
    """

    def __init__(self) -> None:
        self._node_states: dict[str, NodeStatus] = {}
        self._node_outputs: dict[str, dict[str, Any]] = {}
        self._node_errors: dict[str, str] = {}

    # ── 公开接口 ────────────────────────────────────────────────────────────

    async def run(
        self,
        graph: Graph,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        """执行整个图。

        Args:
            graph: 要执行的 Block Graph
            context: 执行上下文（任务级别共享数据）

        Returns:
            ExecutionResult：包含各节点的状态和输出

        Raises:
            GraphValidationError: 图不合法
        """
        # 1. 验证
        errors = graph.validate()
        if errors:
            raise GraphValidationError(errors)

        context = context or ExecutionContext(graph=graph)
        self._reset()

        # 2. 拓扑排序
        exec_order = graph.topological_sort()
        node_map = {n.id: n for n in graph.nodes}

        # 3. 逐层执行（按拓扑序，但同层可并行）
        for node_id in exec_order:
            node = node_map[node_id]
            self._node_states[node_id] = NodeStatus.RUNNING

            try:
                # 3a. 解析输入
                resolved_inputs = self._resolve_inputs(node, graph, context)

                # 3b. 获取 Block 实例
                block_cls = get_block(node.block_id)
                block_instance = block_cls()

                # 3c. 验证 & 创建输入模型
                input_model = block_cls.input_schema(**resolved_inputs)

                # 3d. 执行
                node_outputs: dict[str, Any] = {}
                async for output_name, value in block_instance.run(input_model):
                    node_outputs[output_name] = value
                    # 如果是 progress 消息，也写入 shared_data 供监听用
                    if output_name == "progress":
                        context.shared_data[f"progress:{node_id}"] = value

                # 3e. 保存输出
                self._node_outputs[node_id] = node_outputs
                context.shared_data[node_id] = node_outputs
                self._node_states[node_id] = NodeStatus.COMPLETED

                logger.info(
                    "Node %s (%s) COMPLETED — outputs: %s",
                    node_id, node.block_id, list(node_outputs.keys()),
                )

            except Exception as exc:
                self._node_states[node_id] = NodeStatus.FAILED
                error_msg = _format_error(exc, node_id, node.block_id)
                self._node_errors[node_id] = error_msg
                logger.error("Node %s (%s) FAILED: %s", node_id, node.block_id, error_msg)

                # 默认策略：一旦失败就中止整个图
                break

        return ExecutionResult(
            graph_id=graph.id,
            node_states=dict(self._node_states),
            node_outputs=dict(self._node_outputs),
            node_errors=dict(self._node_errors),
        )

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._node_states = {}
        self._node_outputs = {}
        self._node_errors = {}

    def _resolve_inputs(
        self,
        node: Node,
        graph: Graph,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """解析 Node 的输入：Link 输出 + 常量 input_data + config fallback。

        优先级（高→低）:
            1. 常量 input_data（显式覆盖）
            2. Link 上游输出
            3. node.config 节点级配置
            4. context.config 全局 fallback
        """
        inputs: dict[str, Any] = {}

        # 从 context.config 全局 fallback
        for key, value in context.config.items():
            inputs[key] = value

        # 从 node.config 节点级配置
        for key, value in node.config.items():
            inputs[key] = value

        # 从 Link 上游收集输出
        incoming = graph.get_incoming_links(node.id)
        for link in incoming:
            upstream_output = self._node_outputs.get(link.source_id, {})
            if link.source_output in upstream_output:
                inputs[link.target_input] = upstream_output[link.source_output]

        # 常量 input_data 覆盖
        for key, value in node.input_data.items():
            inputs[key] = value

        return inputs

    def get_node_status(self, node_id: str) -> NodeStatus | None:
        """获取节点当前状态。"""
        return self._node_states.get(node_id)

    def get_node_output(self, node_id: str) -> dict[str, Any]:
        """获取节点输出。"""
        return self._node_outputs.get(node_id, {})


# ── 执行结果 ──────────────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    """图执行结果。"""

    graph_id: str
    node_states: dict[str, NodeStatus]    # node_id → 最终状态
    node_outputs: dict[str, dict[str, Any]]  # node_id → output dict
    node_errors: dict[str, str]           # node_id → 错误消息（仅失败节点）

    @property
    def success(self) -> bool:
        """所有节点都 COMPLETED 才算成功。"""
        return all(
            s == NodeStatus.COMPLETED
            for s in self.node_states.values()
        )

    @property
    def failed_nodes(self) -> list[str]:
        return [nid for nid, s in self.node_states.items() if s == NodeStatus.FAILED]

    def get_output(self, node_id: str, output_name: str, default: Any = None) -> Any:
        """从指定节点获取指定输出。"""
        outputs = self.node_outputs.get(node_id, {})
        return outputs.get(output_name, default)


# ── 工具 ──────────────────────────────────────────────────────────────────────


def _format_error(exc: Exception, node_id: str, block_id: str) -> str:
    """格式化异常消息。"""
    if isinstance(exc, BlockExecutionError):
        return str(exc)
    return f"Node {node_id} ({block_id}) 未预期异常: {type(exc).__name__}: {exc}"
