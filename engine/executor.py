"""DAG 执行引擎 — 调度并执行 Block Graph。

核心职责：
1. 拓扑排序确定执行顺序
2. 解析每个 Node 的输入（Link 上游 + 常量 input_data）
3. 运行 Node 对应 Block 的 run() 方法
4. 管理状态机（PENDING → READY → RUNNING → COMPLETED/FAILED）
5. 收集输出，传递给下游 Node
6. 异常处理：单节点失败可配置降级策略 + 自动重试

Phase 2 新增:
- NodeStateStore: 节点状态持久化到 aiosqlite（断点恢复）
- RetryPolicy: 每节点可配置重试策略（指数退避）
- CallbackConfig: 图执行完成回调
- 版本兼容性检查

不依赖 FastAPI / Celery / Redis，纯 asyncio 实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncGenerator, Awaitable, Callable

import aiosqlite

from engine.block import Block, get_block
from engine.errors import (
    BlockExecutionError,
    GraphValidationError,
    BlockNotFoundError,
    MaxRetriesExceeded,
    VersionMismatchError,
)
from engine.graph import Graph, Link, Node

logger = logging.getLogger(__name__)


# ── 状态枚举 ──────────────────────────────────────────────────────────────────


class NodeStatus(str, Enum):
    PENDING = "pending"       # 初始状态，尚未就绪
    READY = "ready"           # 所有依赖满足，等待执行
    RUNNING = "running"       # 正在执行 run()
    COMPLETED = "completed"   # 执行成功
    FAILED = "failed"         # 执行失败


# ── 重试策略 ──────────────────────────────────────────────────────────────────


@dataclass
class RetryPolicy:
    """节点重试策略。

    Attributes:
        max_retries: 最大重试次数（默认 2，不含首次执行）
        base_delay: 首次退避延迟（秒）
        max_delay: 最大退避延迟（秒）
        backoff_multiplier: 指数乘数
        retryable_exceptions: 可重试的异常类型
        non_retryable_exceptions: 不可重试的异常类型（不会重试，直接失败）
    """
    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_multiplier: float = 2.0
    retryable_exceptions: tuple = (
        BlockExecutionError, TimeoutError, ConnectionError, OSError,
    )
    non_retryable_exceptions: tuple = (
        TypeError, ValueError, KeyError,
        BlockNotFoundError, VersionMismatchError, GraphValidationError,
    )


# ── 回调配置 ──────────────────────────────────────────────────────────────────


@dataclass
class CallbackConfig:
    """图执行完成后的回调配置。"""
    on_complete: Callable[[ExecutionResult], Awaitable[None]] | None = None
    on_fail: Callable[[ExecutionResult], Awaitable[None]] | None = None


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
    callbacks: CallbackConfig | None = None   # 执行完成回调

    # 每节点生命周期回调（被 engine.run() 调用）
    on_node_start: Callable[[str], Awaitable[None]] | None = None     # node_id
    on_node_complete: Callable[[str], Awaitable[None]] | None = None  # node_id
    on_node_fail: Callable[[str, str], Awaitable[None]] | None = None # node_id, error

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


# ── 节点状态持久化 ──────────────────────────────────────────────────────────────


class NodeStateStore:
    """节点状态持久化 — aiosqlite 存储。

    表结构:
        CREATE TABLE IF NOT EXISTS block_node_states (
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            output_json TEXT,
            started_at TEXT,
            completed_at TEXT,
            PRIMARY KEY (graph_id, node_id)
        )
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def _ensure_table(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS block_node_states (
                    graph_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    output_json TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (graph_id, node_id)
                )
            """)
            await db.commit()

    async def save_state(
        self, graph_id: str, node_id: str, block_id: str,
        status: NodeStatus, retry_count: int = 0,
        error: str | None = None, output_json: str | None = None,
    ) -> None:
        """保存节点状态到数据库。

        保留已有的 started_at（节点首次 RUNNING 时设置），
        仅在 COMPLETED/FAILED 时写入 completed_at。
        """
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT started_at FROM block_node_states WHERE graph_id=? AND node_id=?",
                (graph_id, node_id),
            )
            existing = await cursor.fetchone()
            existing_started = existing["started_at"] if existing else None

            now = datetime.now(timezone.utc).isoformat()
            started_at = now if status == NodeStatus.RUNNING and not existing_started else existing_started
            completed_at = now if status in (NodeStatus.COMPLETED, NodeStatus.FAILED) else None

            await db.execute(
                """INSERT OR REPLACE INTO block_node_states
                   (graph_id, node_id, block_id, status, retry_count, error, output_json, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (graph_id, node_id, block_id, status.value, retry_count,
                 error, output_json, started_at, completed_at),
            )
            await db.commit()

    async def load_states(self, graph_id: str) -> dict[str, dict]:
        """恢复已持久化的节点状态（用于断点恢复）。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM block_node_states WHERE graph_id = ?", (graph_id,)
            )
            return {r["node_id"]: dict(r) for r in rows}


# ── 执行引擎 ──────────────────────────────────────────────────────────────────


class ExecutionEngine:
    """Block Graph 执行引擎。

    用法:
        engine = ExecutionEngine()
        result = await engine.run(graph, context, retry_policy)
    """

    def __init__(self) -> None:
        self._node_states: dict[str, NodeStatus] = {}
        self._node_outputs: dict[str, dict[str, Any]] = {}
        self._node_errors: dict[str, str] = {}
        self._store: NodeStateStore | None = None

    # ── 公开接口 ────────────────────────────────────────────────────────────

    async def run(
        self,
        graph: Graph,
        context: ExecutionContext | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> ExecutionResult:
        """执行整个图。

        Args:
            graph: 要执行的 Block Graph
            context: 执行上下文（任务级别共享数据）
            retry_policy: 重试策略（默认不重试）

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

        # 1b. 初始化持久化存储
        if context.db_path:
            self._store = NodeStateStore(context.db_path)

        # 2. 拓扑排序
        exec_order = graph.topological_sort()
        node_map = {n.id: n for n in graph.nodes}

        # 3. 逐层执行
        for node_id in exec_order:
            node = node_map[node_id]
            self._node_states[node_id] = NodeStatus.PENDING

            try:
                # 3a. 版本兼容性检查
                self._check_block_version(node)

                # 3b. 节点开始回调
                if context.on_node_start:
                    await context.on_node_start(node_id)

                # 3c. 执行（含自动重试）
                await self._execute_node_with_retry(node, graph, context, retry_policy)

                # 3d. 节点完成回调
                if context.on_node_complete:
                    await context.on_node_complete(node_id)

            except Exception as exc:
                self._node_states[node_id] = NodeStatus.FAILED
                error_msg = _format_error(exc, node_id, node.block_id)
                self._node_errors[node_id] = error_msg
                await self._persist_state(node, context, block_id=node.block_id, error=error_msg)
                if context.on_node_fail:
                    await context.on_node_fail(node_id, error_msg)
                logger.error("Node %s (%s) FAILED: %s", node_id, node.block_id, error_msg)

                # 默认策略：一旦失败就中止整个图
                break

        result = ExecutionResult(
            graph_id=graph.id,
            node_states=dict(self._node_states),
            node_outputs=dict(self._node_outputs),
            node_errors=dict(self._node_errors),
        )

        # 4. 触发回调
        await self._fire_callbacks(context, result)

        return result

    # ── 节点执行与重试 ────────────────────────────────────────────────────────

    async def _execute_node_with_retry(
        self,
        node: Node,
        graph: Graph,
        context: ExecutionContext,
        retry_policy: RetryPolicy | None,
    ) -> None:
        """执行单个节点，支持指数退避重试。

        返回时表示执行成功。抛出异常表示最终失败。
        """
        if retry_policy is None:
            retry_policy = RetryPolicy(max_retries=0)

        max_retries = retry_policy.max_retries
        retryable = retry_policy.retryable_exceptions

        for attempt in range(max_retries + 1):
            try:
                # 解析输入
                resolved_inputs = self._resolve_inputs(node, graph, context)

                # 获取 Block 实例
                block_cls = get_block(node.block_id)
                block_instance = block_cls()

                # 验证 & 创建输入模型
                input_model = block_cls.input_schema(**resolved_inputs)

                # 设置 RUNNING 状态
                self._node_states[node.id] = NodeStatus.RUNNING
                await self._persist_state(node, context, retry_count=attempt)

                # 执行
                node_outputs: dict[str, Any] = {}
                async for output_name, value in block_instance.run(input_model):
                    node_outputs[output_name] = value
                    if output_name == "progress":
                        context.shared_data[f"progress:{node.id}"] = value

                # 保存输出
                self._node_outputs[node.id] = node_outputs
                context.shared_data[node.id] = node_outputs
                self._node_states[node.id] = NodeStatus.COMPLETED
                await self._persist_state(
                    node, context,
                    retry_count=attempt,
                    output_json=json.dumps(node_outputs, default=str, ensure_ascii=False),
                )
                logger.info(
                    "Node %s (%s) COMPLETED — outputs: %s",
                    node.id, node.block_id, list(node_outputs.keys()),
                )
                return  # 成功

            except retryable as exc:
                error_msg = _format_error(exc, node.id, node.block_id)
                self._node_states[node.id] = NodeStatus.FAILED
                self._node_errors[node.id] = error_msg
                await self._persist_state(
                    node, context, retry_count=attempt + 1, error=error_msg,
                )

                if attempt < max_retries:
                    delay = min(
                        retry_policy.base_delay * (retry_policy.backoff_multiplier ** attempt),
                        retry_policy.max_delay,
                    )
                    logger.warning(
                        "Node %s (%s) attempt %d/%d failed, retrying in %.1fs: %s",
                        node.id, node.block_id, attempt + 1, max_retries + 1, delay, error_msg,
                    )
                    self._node_states[node.id] = NodeStatus.PENDING
                    await asyncio.sleep(delay)
                else:
                    # 重试用尽 → 抛出让外层处理器标记 FAILED
                    raise MaxRetriesExceeded(node.id, node.block_id, max_retries, error_msg)

    # ── 回调 ────────────────────────────────────────────────────────────────────

    async def _fire_callbacks(self, context: ExecutionContext, result: ExecutionResult) -> None:
        """执行完成回调。"""
        if not context.callbacks:
            return
        try:
            if result.success and context.callbacks.on_complete:
                await context.callbacks.on_complete(result)
            elif not result.success and context.callbacks.on_fail:
                await context.callbacks.on_fail(result)
        except Exception as e:
            logger.error("Callback execution failed: %s", e)

    # ── 版本检查 ─────────────────────────────────────────────────────────────

    def _check_block_version(self, node: Node) -> None:
        """检查 Block 版本兼容性。

        如果 node.config 中设置了 expected_version，则与实际注册的 version 比对。
        """
        expected = node.config.get("expected_version")
        if expected:
            block_cls = get_block(node.block_id)
            if block_cls.version != expected:
                raise VersionMismatchError(
                    f"Block {node.block_id} 版本不匹配: 期望 {expected}, 实际 {block_cls.version}"
                )

    # ── 持久化 ──────────────────────────────────────────────────────────────────

    async def _persist_state(
        self, node: Node, context: ExecutionContext,
        block_id: str = "", retry_count: int = 0,
        error: str | None = None, output_json: str | None = None,
    ) -> None:
        if not self._store:
            return
        try:
            status = self._node_states.get(node.id, NodeStatus.PENDING)
            await self._store.save_state(
                graph_id=context.graph.id or "default",
                node_id=node.id,
                block_id=block_id or node.block_id,
                status=status,
                retry_count=retry_count,
                error=error,
                output_json=output_json,
            )
        except Exception as e:
            logger.warning("Failed to persist state for node %s: %s", node.id, e)

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._node_states = {}
        self._node_outputs = {}
        self._node_errors = {}
        self._store = None

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
