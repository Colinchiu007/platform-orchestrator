# 架构补充说明 v2 — Block 编排引擎

> **关联 PRD**: `docs/PRD.md`
> **关联阶段**: Phase 1 (v0.4.x) — Block 基建 + Phase 2 (v0.5.x) — 任务持久化
> **编制日期**: 2026-06-27
> **来源**: [AutoGPT Block 架构](https://github.com/Significant-Gravitas/AutoGPT) 编排理念适配

---

## 一、Why — 为什么需要 Block 编排

### 现状痛点

当前 `services/pipeline.py` 是硬编码的 5 步顺序管道（split → optimize → TTS → image → compose）：

```python
# 当前：线性、紧耦合、不可扩展
scenes = await _run_splitter(content)
prompts = await _run_optimizer(scenes, platform)
audio = await _run_tts(scenes, voice, output_path)
images = await _run_image_gen(prompts, scratch)
result = await _run_compose(images, audio, output_path)
```

| 问题 | 表现 | 影响 |
|------|------|------|
| **扩展困难** | 新增步骤必须改 pipeline.py 核心代码 | 无法让用户自定义流程 |
| **无法编排** | 全部串行，不支持并行或条件分支 | TTS 和图片生成本可并行 |
| **无重试粒度** | 整个管道要么全成要么全败 | 单步失败无法局部重试 |
| **无中间产物可见性** | 每一步结果只存在变量里 | 无法查看或复用中间结果 |
| **无插件生态** | 所有步骤硬编码 | 无法通过配置嵌入第三方步骤 |

### Block 思维带来的转变

Block 将"一段逻辑"封装为**可组合、可复用、可独立测试**的最小单元。借鉴自 AutoGPT Block 架构（Apache 2.0），核心转变：

```
                              ┌──────────────────────┐
  before: 管道 = 顺序函数调用  │    Block 思考：       │
                              │                       │
                              │  split → optimize →  │
    做一件事，加一个函数        │  TTS ──┐  ┌→ compose │
                              │  img ──┴──┘           │
  after:  管道 = 有向无环图    │       可并行 ↓        │
                              │     插件化可插拔        │
                              └──────────────────────┘
```

---

## 二、核心概念

### 2.1 Block

Block 是最小的执行单元，封装一个可复用的业务能力。

```python
class SplitterBlock(Block[SplitterInput, SplitterOutput]):
    id = "splitter"
    name = "智能分句"
    description = "将长文本拆分为场景块，适配语音和画面合成"

    async def run(self, inputs: SplitterInput) -> AsyncGenerator[BlockOutput, None]:
        yield ("progress", json.dumps({"step": "splitting", "progress": 0.1}))
        # ... do work ...
        yield ("scenes", scenes)
```

| 属性 | 说明 |
|------|------|
| `id` | Block 唯一标识（kebab-case） |
| `name` | 人类可读名称 |
| `description` | 功能描述（用于 UI 展示和自动发现） |
| `input_schema` | Pydantic v2 BaseModel — 定义输入数据契约 |
| `output_schema` | Pydantic v2 BaseModel — 定义输出数据契约 |
| `run()` | `AsyncGenerator` — 执行逻辑，异步产出命名结果 |
| `version` | 语义化版本（默认 `"1.0.0"`） |

**设计原则：**
- 每个 Block 只做一件事（单一职责）
- 输入/输出使用 `shared-models` 的 Pydantic v2 模型（跨项目复用）
- `run()` 不直接操作数据库（由执行引擎负责持久化和状态更新）
- 不抛出原始异常，统一通过 `result` yield 或 `BlockExecutionError` 上报

### 2.2 BlockInput / BlockOutput

Block 的输入和输出都是由 Pydantic v2 BaseModel 定义的强类型数据契约：

```python
class SplitterInput(BaseModel):
    content: str = Field(..., description="待拆分的原始文本")
    language: str = Field("zh", description="文本语言")

class SplitterOutput(BaseModel):
    scenes: list[SceneSegment] = Field(..., description="拆分后的场景列表")
    total_scenes: int = Field(..., description="场景总数")
    total_duration_estimate: float = Field(..., description="预估总时长(秒)")

class SceneSegment(BaseModel):
    text: str
    segment_id: int
    estimated_duration: float = 0.0
    sentences: list[str] = []
```

- 输入输出必须是 `BaseModel` 子类（支持 Pydantic v2 validation）
- 可以引用 `shared-models` 中的类型
- 不允许 `Any`、`dict` 等非类型化字段（.clinerules 约束）

### 2.3 Graph / Node / Link

Graph 定义 Block 实例之间的连接关系：

```
Graph
├── nodes: [{id, block_id, input_data, config}]
└── links: [{source_id, source_output, target_id, target_input}]
```

| 组件 | 说明 | 示例 |
|------|------|------|
| **Node** | Block 的具名实例。同一个 Block 类可在图中出现多次 | `{"id": "s1", "block_id": "splitter", "config": {"language": "zh"}}` |
| **Link** | 输入端口的 pin 级连接 | `{"source": "s1", "output": "scenes", "target": "opt1", "input": "content"}` |
| **Graph** | 图的完整描述，包含所有 Node 和 Link | 包含元数据如 `id`、`description`、`version` |

**验证规则：**
- 无环（DAG）：有向无环图，执行器拓扑排序检测环
- 无悬空节点：所有 Link 的 source/target 必须存在于 nodes 中
- 无孤儿节点：所有 Node 的输入必须被满足（来自 Link 或 input_data）
- 类型兼容性：Link 连接的 output 类型与 input 类型匹配

### 2.4 执行器状态机

每个 Node 在 Graph 执行过程中经历 5 种状态：

```
     ┌──────────┐
     │ PENDING  │  ← 初始状态，尚未就绪
     └────┬─────┘
          │ 所有依赖满足
          ▼
     ┌──────────┐
     │  READY   │  ← 等待执行
     └────┬─────┘
          │ 调度
          ▼
     ┌──────────┐
     │ RUNNING  │  ← 正在执行 run()
     └────┬─────┘
          │ 完成 / 失败
          ▼
 ┌──────────────┐
 │  COMPLETED   │  ← 正常完成，产出结果
 │  FAILED      │  ← 执行异常，携带错误信息
 └──────────────┘
```

**依赖规则：** 一个 Node 只有当其所有上游 Link 对应的 source Node 都 `COMPLETED` 后才从 `PENDING` → `READY`。

### 2.5 持久化上下文

Executor 执行时携带 `ExecutionContext`，包含任务级别共享数据：

```python
class ExecutionContext:
    graph: Graph
    job_id: str              # 任务关联 ID
    db_path: str             # SQLite 路径（用于状态更新）
    scratch_dir: str         # 临时文件目录
    config: dict             # 全局配置（voice, ratio, API keys）
    shared_data: dict        # 节点间传递的数据缓存
```

---

## 三、架构图

```
┌─────────────────────────────────────────────────────────────┐
│                     Graph Definition                          │
│                                                               │
│  ┌───────────────┐      ┌───────────────┐                    │
│  │ splitter_node │──────│optimizer_node │                    │
│  │ (SplitBlock)  │ link │(OptBlock)     │                    │
│  └───────┬───────┘      └───────┬───────┘                    │
│          │                      │                            │
│          │              ┌───────┴────────┐                    │
│          │              │  tts_node      │                    │
│          │              │  (TTSBlock)    │                    │
│          │              └───────┬────────┘                    │
│          │              ┌───────┴────────┐                    │
│          └──────────────│  image_node    │                    │
│                         │  (ImgBlock)    │                    │
│                         └───────┬────────┘                    │
│                                 │                            │
│                         ┌───────┴────────┐                    │
│                         │ compose_node   │                    │
│                         │ (ComposeBlock) │                    │
│                         └────────────────┘                    │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Engine                            │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Topo     │→ │ Dispatch │→ │ Node     │→ │ Completion   │  │
│  │ Sort     │  │ Loop     │  │ Executor │  │ Handler      │  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘  │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐   │
│  │  Node State Machine: PENDING→READY→RUNNING→COMPLETED   │   │
│  │                    ┌─────────────────────────┐         │   │
│  │  Error Handling:  │ node-level retry /       │         │   │
│  │  (configurable)   │ graph-level abort / skip │         │   │
│  │                    └─────────────────────────┘         │   │
│  └────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   Concrete Blocks                             │
│                                                               │
│  ┌──────────┐ ┌──────────┐ ┌──────┐ ┌──────────┐ ┌───────┐  │
│  │ Splitter │ │Optimizer │ │ TTS  │ │Image Gen │ │Compose│  │
│  │ Block    │ │ Block    │ │Block │ │ Block    │ │ Block │  │
│  └──────────┘ └──────────┘ └──────┘ └──────────┘ └───────┘  │
│                                                               │
│  Each block wraps existing services/ module functions         │
│  and adds typed I/O schemas + streaming output                │
└───────────────────────────────────────────────────────────────┘
```

### 与现有架构的关系

```
                    ┌─────────────────┐
                    │   FastAPI Routes  │
                    └────────┬────────┘
                             │ 路由调用
                    ┌────────▼────────┐
                    │  Pipeline (v1)  │── 兼容层：pipeline.py 可以内部使用 Block
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Engine         │── Block 执行引擎（新建）
                    │  block.py       │
                    │  graph.py       │
                    │  executor.py    │
                    └────────┬────────┘
                             │ 调用
                    ┌────────▼────────┐
                    │  Blocks         │── Concrete Block 实现（新建）
                    └────────┬────────┘
                             │ 委托
                    ┌────────▼────────┐
                    │  Services (已有) │── collect.py, tts_service.py, etc.
                    └─────────────────┘
```

---

## 四、Block 注册与发现

### 4.1 内置注册表

`engine/__init__.py` 维护全局 Block 注册表：

```python
_BLOCK_REGISTRY: dict[str, type[Block]] = {}

def register_block(block_cls: type[Block]) -> type[Block]:
    """注册 Block 到全局 registry（也可用作装饰器）。"""
    _BLOCK_REGISTRY[block_cls.id] = block_cls
    return block_cls

def get_block(block_id: str) -> type[Block]:
    """按 ID 获取 Block 类。"""
    if block_id not in _BLOCK_REGISTRY:
        raise ValueError(f"Block '{block_id}' not registered")
    return _BLOCK_REGISTRY[block_id]

def list_blocks() -> list[dict]:
    """列出所有已注册 Block 的元信息。"""
    return [
        {"id": cls.id, "name": cls.name, "description": cls.description}
        for cls in _BLOCK_REGISTRY.values()
    ]
```

### 4.2 自动发现

`blocks/__init__.py` 在导入时自动加载并注册所有 Block 类：

```python
# blocks/__init__.py
from engine import register_block
from blocks.splitter_block import SplitterBlock
from blocks.optimizer_block import OptimizerBlock
# ... etc

register_block(SplitterBlock)
register_block(OptimizerBlock)
```

### 4.3 用户自定义 Block

任何 Python 模块只需：
1. 继承 `Block[InputModel, OutputModel]`
2. 定义 `input_schema` / `output_schema`
3. 实现 `run()`
4. 调用 `register_block(MyBlock)` 注册

不修改核心代码，开箱即用。

---

## 五、迁移路线

### Phase 1 — Block 基建（v0.4.x，当前）

| 任务 | 交付物 | 说明 |
|------|--------|------|
| ✅ Block 基类 | `engine/block.py` | ABC + 泛型 + AsyncGenerator run + 注册表 |
| ✅ Graph 模型 | `engine/graph.py` | Graph/Node/Link 数据模型 + DAG 验证 |
| ✅ 执行引擎 | `engine/executor.py` | DAG 调度 + 状态机 + 错误处理 |
| ✅ 错误类型 | `engine/errors.py` | BlockExecutionError + GraphValidationError |
| ✅ 示例 Blocks | `blocks/*.py` | 包装 pipeline.py 现有 5 步 |
| ✅ 架构文档 | `docs/architecture-v2.md` | 本文档 |

### Phase 2 — Block 管线化（v0.5.x）

| 任务 | 说明 |
|------|------|
| `pipeline.py` 改用 Block 调用 | 内部重写为 Block Graph 执行 |
| 任务持久化 | 节点状态写入 `aiosqlite` |
| 失败重试 | 单节点粒度重试策略 |
| 回调通知 | 完成/失败回调注册机制 |
| Block 版本管理 | 支持 Block 版本兼容性检查 |

### Phase 3 — 可视化编排（v1.0.x）

| 任务 | 说明 |
|------|------|
| Block 可视化 | 前端展示 Block Graph 拓扑 |
| 拖拽编排 | 用户可自定义管线流程 |
| Block 市场 | 注册第三方 Block 插件 |
| 条件分支 | 支持 if/else 条件节点 |

### 向后兼容策略

- `services/pipeline.py` **不删除**，保持完整功能
- `main.py` 的路由保持不变（新老共存）
- Engine 模块不修改任何被引用模块代码（.clinerules 零侵入约束）
- 新增的 `engine/` 和 `blocks/` 包不依赖 FastAPI，可独立测试

---

## 六、与 AutoGPT 的差异

> 根据 `.clinerules` 引入策略要求，标注来源及差异。

| 方面 | AutoGPT Block | 本项目适配 |
|------|---------------|-----------|
| **输入/输出** | Generic ABCBlock[T] | Pydantic v2 BaseModel 双泛型 |
| **run() 返回** | `AsyncGenerator[tuple[str, Any]]` | 同样模式，保持兼容 |
| **图模型** | AgentGraph / AgentNode / AgentLink | 简化版 Graph / Node / Link |
| **执行引擎** | 完整的 AgentServer 调度 | 轻量 asyncio 执行，不引入 Redis/Celery |
| **Block 注册** | 装饰器 `@restricted` / `@no_cache` | 简单装饰器 `@register_block` |
| **持久化** | PostgreSQL + Prisma | aiosqlite (WAL) |
| **资源需求** | 完整的 WebSocket 服务端 | 无新增服务（.clinerules 约束） |

**许可证**：AutoGPT 使用 MIT License。本适配保留了核心设计思路，代码为独立实现。

---

## 七、文件变更清单

| 文件 | 操作 |
|------|------|
| `platform-orchestrator/docs/architecture-v2.md` | **新增** — 本文档 |
| `platform-orchestrator/engine/__init__.py` | **新增** — 包初始化 + 注册表 |
| `platform-orchestrator/engine/block.py` | **新增** — Block 基类 |
| `platform-orchestrator/engine/graph.py` | **新增** — 图模型 |
| `platform-orchestrator/engine/executor.py` | **新增** — 执行引擎 |
| `platform-orchestrator/engine/errors.py` | **新增** — 异常类型 |
| `platform-orchestrator/blocks/__init__.py` | **新增** — Block 自动注册 |
| `platform-orchestrator/blocks/splitter_block.py` | **新增** — 分句 Block |
| `platform-orchestrator/blocks/optimizer_block.py` | **新增** — 提示词优化 Block |
| `platform-orchestrator/blocks/tts_block.py` | **新增** — 语音合成 Block |
| `platform-orchestrator/blocks/image_gen_block.py` | **新增** — 图片生成 Block |
| `platform-orchestrator/blocks/compose_block.py` | **新增** — 视频合成 Block |
| `platform-orchestrator/pyproject.toml` | **修改** — 添加 engine/ 和 blocks/ 包路径 |
| `platform-orchestrator/tests/test_engine.py` | **新增** — 单元测试 |
