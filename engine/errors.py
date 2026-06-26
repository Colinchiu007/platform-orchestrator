"""异常类型 — Block 引擎专用异常。

所有异常继承自 ``BlockEngineError``，外层可用 ``except BlockEngineError``
统一捕获（但仍建议按具体类型分别处理）。
"""

from __future__ import annotations

from typing import Any


class BlockEngineError(Exception):
    """Block 引擎基础异常。"""
    pass


class BlockExecutionError(BlockEngineError):
    """Block 执行失败。

    Attributes:
        block_id: 失败的 Block ID
        node_id: 失败的 Node ID（如果有）
        reason: 失败原因描述
        cause: 原始异常（如果有）
    """

    def __init__(
        self,
        block_id: str,
        node_id: str | None = None,
        reason: str = "",
        cause: Exception | None = None,
    ) -> None:
        self.block_id = block_id
        self.node_id = node_id
        self.reason = reason
        self.cause = cause
        msg = f"[{node_id or block_id}] {reason}"
        if cause:
            msg += f" ({type(cause).__name__}: {cause})"
        super().__init__(msg)


class GraphValidationError(BlockEngineError):
    """图验证失败 — 包含详细的违规列表。

    Attributes:
        errors: 每条违规描述的列表
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        msg = "图验证失败:\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


class BlockNotFoundError(BlockEngineError):
    """Block 类型未注册。"""
    pass


class NodeNotFoundError(BlockEngineError):
    """图中未找到指定 Node。"""
    pass


class LinkError(BlockEngineError):
    """Link 连接异常（类型不匹配等）。"""
    pass
