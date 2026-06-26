"""Graph / Node / Link 数据模型 — 定义 Block 编排的有向无环图 (DAG)。

Graph 是 Block 编排的核心抽象：
- Node 是 Block 的具名实例
- Link 定义 Node 间输入/输出端口的连接关系
- Graph 提供 DAG 验证（环检测、悬空节点、孤儿节点）

来源适配:
    AutoGPT AgentGraph / AgentNode / AgentLink (MIT License)
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field, field_validator

from engine.errors import GraphValidationError


class Node(BaseModel):
    """图中 Block 实例。

    Attributes:
        id: 节点唯一标识（图中唯一）
        block_id: 对应注册的 Block ID
        input_data: 常量输入（覆盖 Link 提供的值，优先级: input_data > Link）
        config: Block 运行时配置（传给 Block 构造函数）
        metadata: 节点元信息（如坐标、描述等，仅供前端展示）
    """

    id: str = Field(..., description="节点唯一标识")
    block_id: str = Field(..., description="注册的 Block ID")
    input_data: dict[str, Any] = Field(
        default_factory=dict, description="常量输入映射"
    )
    config: dict[str, Any] = Field(
        default_factory=dict, description="Block 运行时配置"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="节点元信息（仅供展示）"
    )


class Link(BaseModel):
    """Node 间的连接 — pin 级路由。

    连接的语义：source[soure_output] → target[target_input]

    Attributes:
        source_id: 输出方 Node ID
        source_output: 输出方 yield 的 output_name
        target_id: 输入方 Node ID
        target_input: 输入方 input_schema 的字段名
    """

    source_id: str = Field(..., description="输出方 Node ID")
    source_output: str = Field(..., description="输出方 yield 的 output_name")
    target_id: str = Field(..., description="输入方 Node ID")
    target_input: str = Field(..., description="输入方 input_schema 字段名")


class Graph(BaseModel):
    """Block 编排图。

    Attributes:
        id: 图唯一标识
        description: 图描述
        nodes: 节点列表
        links: 连接列表
        metadata: 图级元信息
    """

    id: str = Field(default="", description="图唯一标识")
    description: str = Field(default="", description="图描述")
    nodes: list[Node] = Field(default_factory=list, description="节点列表")
    links: list[Link] = Field(default_factory=list, description="连接列表")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="图级元信息"
    )

    # ── 验证 ──────────────────────────────────────────────────────────────────

    @field_validator("nodes")
    @classmethod
    def _check_unique_node_ids(cls, nodes: list[Node]) -> list[Node]:
        ids = [n.id for n in nodes]
        if len(ids) != len(set(ids)):
            duplicates = [nid for nid in ids if ids.count(nid) > 1]
            raise ValueError(f"Node ID 重复: {set(duplicates)}")
        return nodes

    @field_validator("links")
    @classmethod
    def _check_unique_links(cls, links: list[Link]) -> list[Link]:
        seen: set[tuple[str, str, str, str]] = set()
        for link in links:
            key = (link.source_id, link.source_output, link.target_id, link.target_input)
            if key in seen:
                raise ValueError(f"Link 重复: {key}")
            seen.add(key)
        return links

    # ── 拓扑分析 ──────────────────────────────────────────────────────────────

    def topological_sort(self) -> list[str]:
        """DAG 拓扑排序，返回 Node ID 列表。

        Raises:
            GraphValidationError: 如果存在环
        """
        in_degree: dict[str, int] = {n.id: 0 for n in self.nodes}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for link in self.links:
            adjacency[link.source_id].append(link.target_id)
            in_degree[link.target_id] = in_degree.get(link.target_id, 0) + 1

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        result: list[str] = []

        while queue:
            node_id = queue.popleft()
            result.append(node_id)
            for neighbor in adjacency[node_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self.nodes):
            cycle_nodes = set(self._node_ids()) - set(result)
            raise GraphValidationError([
                f"检测到环，涉及节点: {cycle_nodes}"
            ])

        return result

    def validate(self) -> list[str]:
        """完整验证图合法性。

        Returns:
            违规描述列表。空列表表示图合法。
        """
        errors: list[str] = []
        node_ids = set(self._node_ids())

        # 1. 节点不能为空
        if not self.nodes:
            errors.append("图中没有节点")

        # 2. Link 的 source/target 必须在 nodes 中
        for i, link in enumerate(self.links):
            if link.source_id not in node_ids:
                errors.append(f"Link[{i}]: source_id={link.source_id!r} 不存在于 nodes 中")
            if link.target_id not in node_ids:
                errors.append(f"Link[{i}]: target_id={link.target_id!r} 不存在于 nodes 中")

        # 3. 环检测
        try:
            self.topological_sort()
        except GraphValidationError as e:
            errors.extend(e.errors)

        return errors

    def _node_ids(self) -> list[str]:
        return [n.id for n in self.nodes]

    def get_node(self, node_id: str) -> Node | None:
        """按 ID 查找节点。"""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_outgoing_links(self, node_id: str) -> list[Link]:
        """获取节点的所有输出连接。"""
        return [l for l in self.links if l.source_id == node_id]

    def get_incoming_links(self, node_id: str) -> list[Link]:
        """获取节点的所有输入连接。"""
        return [l for l in self.links if l.target_id == node_id]
