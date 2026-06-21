# platform-orchestrator — 统一平台入口

> 薄壳 FastAPI 应用，整合 5 个独立模块为完整视频生成平台。

## 定位

`platform-orchestrator` 是整个产品体系的**唯一运行进程**。它通过 editable install 引用 5 个独立模块，提供统一的 REST API、JWT 鉴权、功能开关、任务调度。

**核心原则**：薄壳模式。orchestrator 只做路由、鉴权、编排，不包含任何业务逻辑。所有业务能力由被引用的模块提供。

## 架构

```
                    platform-orchestrator (1 进程，~150MB)
                    ├── JWT 鉴权中间件
                    ├── 功能开关装饰器 (@requires_feature)
                    ├── SQLite 任务状态数据库
                    │
        ┌───────────┼───────────┬───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
  aggregator    splitter   prompt-engine Story2Video  Multi-Publish
  (import)      (import)     (import)      (import)     (import)

  所有模块：editable install，同进程内函数调用，零网络开销
```

## 快速启动

```bash
cd /srv/projects/platform-orchestrator

# 安装 orchestrator 自身及其依赖
pip install -e .

# 安装所有模块（editable install）
pip install -e ../shared-models/
pip install -e ../content-aggregator/
pip install -e ../smart-sentence-splitter/
pip install -e ../prompt-engine/
pip install -e ../Story2Video/         # 待 Phase 2
pip install -e ../Multi-Publish/       # 待 Phase 3

# 启动（开发模式）
uvicorn main:app --reload --port 8000

# 访问
open http://localhost:8000/docs        # OpenAPI 文档
open http://localhost:8000/health      # 健康检查
```

## API 端点

### 核心端点

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|:----:|
| GET | `/health` | 健康检查 | ❌ |
| GET | `/api/features` | 功能开关列表 | ❌ |
| POST | `/api/auth/register` | 用户注册 | ❌ |
| POST | `/api/auth/login` | 登录获取 JWT | ❌ |

### 文章管理（Phase 1）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/articles/` | 文章列表 |
| GET | `/api/articles/{id}` | 文章详情 |
| POST | `/api/articles/{id}/split` | 分句 |

### 提示词优化（Phase 2）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/prompts/optimize` | 优化提示词 |
| POST | `/api/prompts/classify` | 风格分类 |

### 视频生成（Phase 2）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/jobs/video` | 创建视频任务 |
| GET | `/api/jobs/video/{id}` | 查询任务进度 |

### 发布管理（Phase 3）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/jobs/publish` | 创建发布任务 |
| GET | `/api/jobs/publish/{id}` | 查询发布状态 |

## 功能开关

所有付费功能通过 `feature_gates.yaml` 控制，无需修改代码。

```yaml
# 示例
features:
  split_batch:
    tier: 2  # 高级版才能使用批量分句
```

代码中使用装饰器：

```python
from middleware.feature_gate import requires_feature

@router.post("/articles/{id}/split")
@requires_feature("split_single")  # 入门版可用
async def split_article(id: str): ...

@router.post("/articles/batch-split")
@requires_feature("split_batch")   # 仅高级版
async def batch_split(ids: list[str]): ...
```

## 目录结构

```
platform-orchestrator/
├── main.py              # FastAPI 应用入口
├── config.py            # 配置管理（pydantic-settings，环境变量 PO_*）
├── db.py                # aiosqlite 数据库（WAL 模式，自动建表）
├── middleware/
│   ├── auth.py          # JWT 鉴权（HS256，get_current_user 依赖）
│   └── feature_gate.py  # 功能开关（@requires_feature 装饰器）
├── routers/             # 每个模块一个路由文件
│   ├── aggregator.py    # /api/articles/*
│   ├── splitter.py      # /api/articles/{id}/split
│   ├── prompt.py        # /api/prompts/*
│   ├── video.py         # /api/jobs/video/*
│   └── publish.py       # /api/jobs/publish/*
└── pyproject.toml       # 依赖声明
```

## 资源约束

| 指标 | 目标 |
|------|------|
| 常驻内存 | <200MB |
| 峰值内存（视频任务） | <800MB |
| 数据库 | SQLite（WAL 模式） |
| 并发视频任务 | 1（严格串行） |
| 服务器 | 4G 阿里云 ECS |

## 环境变量

所有配置可通过 `PO_` 前缀的环境变量覆盖：

```bash
export PO_SECRET_KEY="my-production-secret"
export PO_DEBUG=true
export PO_CORS_ORIGINS='["https://myapp.com"]'
```

## 版本

**0.1.0** — Phase 0：架构骨架 + 共享模型 + 功能开关。
