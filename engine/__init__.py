"""Block 引擎 — 初始化包。

公开 API:
- Block, BlockOutput — 基类
- register_block, get_block, list_blocks, clear_registry — 注册表
- Graph, Node, Link — 图模型
- ExecutionEngine, ExecutionContext, ExecutionResult — 执行引擎
- BlockExecutionError, GraphValidationError — 异常
"""

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
    BlockEngineError,
    GraphValidationError,
    LinkError,
    NodeNotFoundError,
)
from engine.executor import (
    ExecutionContext,
    ExecutionEngine,
    ExecutionResult,
    NodeStatus,
)
from engine.graph import Graph, Link, Node

__all__ = [
    # Block
    "Block",
    "BlockOutput",
    "register_block",
    "get_block",
    "list_blocks",
    "clear_registry",
    # Graph
    "Graph",
    "Node",
    "Link",
    # Executor
    "ExecutionEngine",
    "ExecutionContext",
    "ExecutionResult",
    "NodeStatus",
    # Errors
    "BlockExecutionError",
    "BlockNotFoundError",
    "BlockEngineError",
    "GraphValidationError",
    "LinkError",
    "NodeNotFoundError",
]
