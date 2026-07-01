"""Block 基类 — 借鉴 AutoGPT Block 编排理念的通用执行单元。

每个 Block 封装一个可复用的业务能力，通过 Pydantic v2 定义输入/输出数据契约，
通过 AsyncGenerator 流式产出结果，支持组合为有向无环图 (DAG) 执行。

来源适配:
    AutoGPT Block 架构 (MIT License)
    https://github.com/Significant-Gravitas/AutoGPT
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Generic, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── 泛型类型 ──────────────────────────────────────────────────────────────────

Input = TypeVar("Input", bound=BaseModel)
Output = TypeVar("Output", bound=BaseModel)

# Stream 产出: (output_name: str, value: Any)
BlockOutput = tuple[str, Any]


# ── Block 基类 ────────────────────────────────────────────────────────────────


class Block(ABC, Generic[Input, Output]):
    """Block 抽象基类。

    所有业务 Block 继承此类，实现 ``run()`` 方法即可。
    输入/输出通过 Pydantic v2 BaseModel 定义类型契约。

    Type Parameters:
        Input: 输入数据的 Pydantic v2 模型类型
        Output: 输出数据的 Pydantic v2 模型类型
    """

    # ── 元数据（子类覆写） ────────────────────────────────────────────────────

    id: str = ""                     # Block 唯一标识（kebab-case）
    name: str = ""                   # 人类可读名称
    description: str = ""            # 功能描述
    version: str = "1.0.0"           # 语义化版本

    input_schema: type[BaseModel] = BaseModel   # 输入模型类（子类覆写）
    output_schema: type[BaseModel] = BaseModel  # 输出模型类（子类覆写）

    # ── 构造 ──────────────────────────────────────────────────────────────────

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """自动验证子类必须定义 id 和 input/output_schema。"""
        super().__init_subclass__(**kwargs)
        if not cls.id:
            cls.id = cls.__name__.lower().replace("block", "")
        if cls.input_schema is BaseModel:
            logger.debug(
                "Block %s: input_schema 未覆写，使用默认 BaseModel", cls.__name__
            )
        if cls.output_schema is BaseModel:
            logger.debug(
                "Block %s: output_schema 未覆写，使用默认 BaseModel", cls.__name__
            )

    # ── 核心接口 ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def run(self, inputs: Input) -> AsyncGenerator[BlockOutput, None]:
        """执行 Block 逻辑。

        Args:
            inputs: Pydantic v2 验证过的输入实例

        Yields:
            (output_name, value) 元组。output_name 是输出字段名，
            value 可以是任意 JSON 可序列化值。

        Raises:
            BlockExecutionError: 执行失败时由 executor 统一处理
        """
        yield ...   # pragma: no cover

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """返回 Block 元信息，用于注册表枚举和前端展示。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "input_schema": self._schema_to_dict(self.input_schema),
            "output_schema": self._schema_to_dict(self.output_schema),
        }

    @staticmethod
    def _schema_to_dict(schema: type[BaseModel]) -> dict[str, Any]:
        """将 Pydantic schema 转为可序列化的描述字典。"""
        try:
            return schema.model_json_schema()
        except Exception:
            return {"type": "object", "description": str(schema)}

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id!r}>"


# ── 注册表 ────────────────────────────────────────────────────────────────────

_BLOCK_REGISTRY: dict[str, type[Block]] = {}


def register_block(block_cls: type[Block]) -> type[Block]:
    """注册 Block 类到全局注册表。

    可作为装饰器使用：

        @register_block
        class MyBlock(Block[In, Out]):
            ...
    """
    if not issubclass(block_cls, Block):
        raise TypeError(f"{block_cls.__name__} 不是 Block 子类")

    block_id = block_cls.id
    if block_id in _BLOCK_REGISTRY:
        logger.warning("Block %r 已被注册，覆盖", block_id)

    _BLOCK_REGISTRY[block_id] = block_cls
    logger.info("Registered block: %s (%s)", block_id, block_cls.__name__)
    return block_cls


def get_block(block_id: str) -> type[Block]:
    """按 ID 获取 Block 类。"""
    if block_id not in _BLOCK_REGISTRY:
        raise ValueError(f"Block '{block_id}' 未注册")
    return _BLOCK_REGISTRY[block_id]


def list_blocks() -> list[dict[str, Any]]:
    """列出所有已注册 Block 的元信息。"""
    return [cls().to_dict() for cls in _BLOCK_REGISTRY.values()]


def clear_registry() -> None:
    """清空注册表（仅测试用）。"""
    _BLOCK_REGISTRY.clear()
