# PROJECT-000：platform-orchestrator — 统一入口薄壳 — PRD

> **立项日期**: 2026-06-03
> **最后更新**: 2026-06-27
> **当前版本**: v0.3.2（Phase 0 已部署）
> **产品定位**: "一站式视频生成平台"的统一入口，薄壳整合所有子模块，提供路由转发、统一鉴权、功能开关和异步编排能力
> **目标用户**: 自媒体创作者、视频运营团队、内容生产者
> **技术架构**: FastAPI + Python SDK 同进程导入 + aiosqlite + PostgreSQL + Nginx 反向代理

---

## 一、产品概述

### 1.1 核心价值

"一站式视频生成平台"由 6+ 个独立子模块（TrendScope、Content-Aggregator、Smart-Sentence-Splitter、Prompt-Engine、Story2Video、Multi-Publish）组成。platform-orchestrator 作为**薄壳统一入口**，提供：

1. **单一入口**：用户通过一个 API 地址（:8000）和一个 Web 前端（:3000）访问所有功能，无需记忆多端口
2. **统一认证**：JWT 单点登录，一次登录访问所有子模块
3. **功能开关**：通过 `feature_gates.yaml` 动态控制各模块可用性，支持分级付费
4. **零开销集成**：所有子模块通过 `pip install -e` 导入为 Python SDK，同进程调用，无网络延迟
5. **异步编排**：长耗时任务（视频合成、AI 改写）通过 `BackgroundTasks` 串行执行
6. **资源可控**：严格的内存和并发约束，适配 4G 阿里云 ECS

### 1.2 产品边界

| 范围 | 说明 |
|------|------|
| ✅ 路由转发 | 所有 `/api/*` 请求统一分发到对应子模块处理器 |
| ✅ 统一认证 | JWT 鉴权（HS256），单点登录，跨模块共享 Token |
| ✅ 功能开关 | YAML 配置化，支持按订阅等级控制功能可用性 |
| ✅ 模块集成 | Python SDK 同进程导入，零网络开销（Phase 1） |
| ✅ 异步任务 | BackgroundTasks 编排，视频任务严格串行 |
| ✅ 统一前端 | Next.js 应用（:3000），Nginx 代理整合 |
| ✅ 数据库 | aiosqlite（WAL 模式）+ PostgreSQL（共享） |
| ❌ 不包含 | 内容创作（归 Content-Aggregator）、热榜采集（归 TrendScope）、视频渲染（归 Story2Video）、多平台发布（归 Multi-Publish） |

> **设计原则**：orchestrator 是"薄壳"——只做路由、鉴权、编排，不做业务逻辑。所有业务功能由各子模块独立完成。

---

## 二、平台策略

### 2.1 集成模块矩阵

| 子模块 | 类型 | 集成方式 | 路由前缀 | 依赖 shared-models | Phase |
|--------|------|----------|----------|-------------------|-------|
| **TrendScope** | 热榜聚合 | SDK 导入 | `/api/trending` | TrendingTopicModel | Phase 0 ✅ |
| **Content-Aggregator** | 内容采集改写 | SDK 导入 | `/api/articles` | ContentFetchRequest | Phase 0 ✅ |
| **Smart-Sentence-Splitter** | 智能分句 | SDK 导入 | `/api/articles/*/split` | SentenceBlock | Phase 0 ✅ |
| **Prompt-Engine** | 提示词优化 | SDK 导入 | `/api/prompts` | OptimizeRequest | Phase 0 ✅ |
| **Story2Video** | 文字转视频 | SDK 导入 | `/api/jobs/video` | (内部模型) | Phase 0 ✅ |
| **Multi-Publish** | 多平台发布 | SDK 导入 | `/api/jobs/publish` | (内部模型) | Phase 0 ✅ |

### 2.2 技术路线

所有子模块通过 `pip install -e .` 安装为可编辑包，orchestrator 直接 `import` 调用其 Python SDK。
**不引入多进程 / 微服务 / HTTP 调用**，保持零网络开销。Future Phase 将引入 BackgroundTasks 异步编排长耗时任务。

---

## 三、功能需求

### 3.1 核心功能

#### F1：统一路由转发

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 健康检查 | `GET /health` 返回服务状态和版本 | ✅ |
| 功能列表 | `GET /api/features` 列出所有功能开关状态 | ✅ |
| 子模块路由 | 以 `/api/articles`、`/api/prompts`、`/api/jobs` 等前缀分发 | ✅ |
| 静态资源 | `/static` 挂载静态文件目录 | ✅ |
| CORS | 本地开发前端（:5173、:3000）跨域支持 | ✅ |

#### F2：统一认证（SSO）

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 用户注册 | 用户名 / 邮箱 / 密码注册（bcrypt 加密） | ✅ |
| 用户登录 | 返回 JWT access + refresh token | ✅ |
| Token 刷新 | refresh token 换发新的 access token（30 天有效） | ✅ |
| 鉴权中间件 | `get_current_user` 依赖注入，保护私有端点 | ✅ |
| 订阅等级 | 用户 tier 字段控制功能访问权限 | ✅ |
| 数据库 | PostgreSQL（auth 表）+ SQLite 本地开发回退 | ✅ |

#### F3：功能开关（Feature Gates）

| 子功能 | 描述 | 状态 |
|--------|------|------|
| YAML 配置 | `feature_gates.yaml` 定义所有功能开关 | ✅ |
| 装饰器 | `@requires_feature` 装饰器绑定到路由 | ✅ |
| 分级控制 | tier 1~4 对应免费/basic/pro/enterprise 订阅 | ✅ |
| 热加载 | 修改 YAML 后重启生效，无需改代码 | ✅ |
| 模块颗粒度 | 每个子模块的关键功能独立控制 | ✅ |

当前定义的功能开关：

| 开关名 | tier | enabled | 说明 |
|--------|------|---------|------|
| `trending_feed` | 1 | true | 热榜展示（免费功能） |
| `trending_to_pipeline` | 2 | true | 热榜自动送入内容管道 |
| `article_manual_fetch` | 1 | true | 手动采集文章 |
| `article_auto_fetch` | 2 | true | 自动采集文章 |
| `split_single` | 1 | true | 单句拆分 |
| `split_batch` | 2 | true | 批量拆分 |
| `prompt_optimize` | 1 | true | 提示词优化 |
| `prompt_classify` | 1 | true | 提示词分类 |
| `video_fixed_template` | 2 | true | 固定模板视频 |
| `video_full_pipeline` | 3 | false | 全流水线视频（未开放） |
| `video_concurrency_control` | 1 | true | 视频串行控制 |
| `publish_single_platform` | 2 | true | 单平台发布 |
| `publish_multi_platform` | 3 | false | 多平台发布（未开放） |

#### F4：子模块 SDK 集成

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 同进程导入 | pip install -e 安装各模块，直接 import 调用 | ✅ |
| 零网络开销 | 函数调用，无 HTTP/序列化开销 | ✅ |
| 模块隔离 | 各模块保持独立 Git 仓库和独立部署能力 | ✅ |
| 零侵入 | orchestrator 不修改被引用模块的代码 | ✅ |

#### F5：异步任务编排（Phase 1+）

| 子功能 | 描述 | 状态 |
|--------|------|------|
| BackgroundTasks | FastAPI 内置异步任务执行 | ✅ 基础支持 |
| 视频串行 | 同时只允许 1 个视频合成任务，FIFO 队列 | ✅ |
| 内容管道 | 趋势发现 → 采集 → 改写 → 分句 → 提示词 → 视频的全流程编排 | 📅 Phase 1 |

### 3.2 非功能需求

| 需求 | 指标 | 状态 |
|------|------|------|
| **常驻内存** | < 200MB（idle） | ✅ |
| **峰值内存** | < 800MB（视频任务时） | ✅ |
| **并发视频任务** | 1（严格串行，FIFO 队列） | ✅ |
| **数据库** | aiosqlite（WAL 模式）+ PostgreSQL 15 | ✅ |
| **认证算法** | HS256，共享 PO_SECRET_KEY 环境变量 | ✅ |
| **静态文件** | /static 挂载 | ✅ |
| **端口** | 8000（API） | ✅ |
| **CORS** | 支持本地开发前端跨域 | ✅ |
| **部署** | 4G 阿里云 ECS，Nginx 反向代理 | ✅ |
| **进程保活** | systemd 服务，自动重启 | ✅ |

---

## 四、技术架构

### 4.1 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户 (Browser / API Client)                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                     ┌─────▼─────┐
                     │   Nginx   │   :80 / :443
                     │ 反向代理   │
                     └─────┬─────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         ┌────▼────┐ ┌────▼────┐  ┌───▼────┐
         │ /api/*  │ │   /     │  │ /static│
         │→:8000   │ │→:3000   │  │ 本地   │
         └────┬────┘ │ (Next.js│  └────────┘
              │      │  前端)  │
              │      └─────────┘
              ▼
┌──────────────────────────────────────────────────────────────────┐
│                  FastAPI (port 8000)                               │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                     Middleware                                │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │  │
│  │  │ CORS         │  │ Rate Limit   │  │ Static Files     │  │  │
│  │  └──────────────┘  └──────────────┘  └──────────────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                     Routers (/routers/)                       │  │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐  │  │
│  │  │ Auth   │ │Payment │ │Trending│ │Articles│ │ Splitter │  │  │
│  │  │ /auth  │ │/pay    │ │/trend  │ │ /api   │ │ /split   │  │  │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └──────────┘  │  │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐  │  │
│  │  │ Prompt │ │ Video  │ │Publish │ │Web     │ │Dashboard │  │  │
│  │  │ /prompt│ │ /jobs  │ │ /jobs  │ │ /      │ │ /dash    │  │  │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └──────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                     Services (/services/)                     │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │  │
│  │  │ Pipeline │ │Compositor│ │ Rewrite  │ │ Concurrency  │  │  │
│  │  │ 编排引擎  │ │ 合成引擎  │ │ 改写引擎  │ │ 并发控制     │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────────┘  │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │  │
│  │  │Image     │ │ TTS      │ │Story2Video│ │ Publish      │  │  │
│  │  │ 图片服务  │ │ 语音服务  │ │ 视频管线   │ │ 发布服务     │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │              Python SDK Imports (pip install -e)              │  │
│  │  ┌─────────────┐  ┌──────────────  ┌──────────────────┐  │  │
│  │  │ trendscope  │  │content-agg    │ smart-sentence-  │  │  │
│  │  │ (热榜引擎)   │  │ (采集改写)     │ splitter(分句)   │  │  │
│  │  └─────────────┘  └──────────────  └──────────────────┘  │  │
│  │  ┌─────────────┐  ┌──────────────  ┌──────────────────┐  │  │
│  │  │prompt-engine │  │ Story2Video   │  Multi-Publish   │  │  │
│  │  │ (提示词优化) │  │ (视频合成)     │  (多平台发布)    │  │  │
│  │  └─────────────┘  └──────────────  └──────────────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                      Data Layer                               │  │
│  │  ┌──────────────────┐  ┌────────────────────────────────┐  │  │
│  │  │  aiosqlite (WAL)  │  │  PostgreSQL 15 (auth/tier)    │  │  │
│  │  │  orchestrator.db  │  │  shared with trendscope       │  │  │
│  │  └──────────────────┘  └────────────────────────────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 目录结构

```
platform-orchestrator/
├── main.py                    # FastAPI 入口，路由注册
├── config.py                  # 配置（pydantic-settings, PO_ 前缀）
├── db.py                      # aiosqlite 初始化（WAL 模式）
├── db_pg.py                   # PostgreSQL 初始化（auth 表）
│
├── middleware/
│   ├── auth.py                # JWT 鉴权中间件
│   ├── feature_gate.py        # 功能开关中间件
│   └── rate_limit.py          # 限流中间件
│
├── models/
│   ├── __init__.py
│   └── auth_models.py         # 认证数据结构
│
├── routers/                   # API 路由模块
│   ├── aggregator.py          # 内容采集改写路由
│   ├── auth.py                # 认证路由（注册/登录/刷新）
│   ├── dashboard.py           # Dashboard 路由
│   ├── payment.py             # 支付路由
│   ├── prompt.py              # 提示词优化路由
│   ├── publish.py             # 多平台发布路由
│   ├── splitter.py            # 智能分句路由
│   ├── trending.py            # 热榜路由
│   ├── video.py               # 视频合成路由
│   └── web.py                 # 前端页面路由
│
├── services/                  # 业务服务层
│   ├── collect.py             # 内容采集服务
│   ├── compositor.py          # 合成引擎
│   ├── concurrency_control.py # 并发控制（视频串行）
│   ├── image_service.py       # 图片处理服务
│   ├── pipeline.py            # 全流程编排引擎
│   ├── prompt_service.py      # 提示词服务
│   ├── publish_service.py     # 发布服务
│   ├── rewrite.py             # 内容改写服务
│   ├── tts_service.py         # 语音合成服务
│   ├── video_service.py       # 视频合成服务
│   └── story2video/           # Story2Video 子模块
│       ├── audio_mixer.py     # 音频混音
│       ├── pipeline.py        # 视频管道
│       ├── slideshow.py       # 幻灯片生成
│       └── text_segmentation.py  # 文本分段
│
├── tests/                     # 测试套件
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_concurrency.py
│   ├── test_e2e_pipeline.py
│   ├── test_feature_gate.py
│   ├── test_payment.py
│   ├── test_prompt.py
│   ├── test_rate_limit.py
│   ├── test_subscription.py
│   ├── test_trendscope_integration.py
│   ├── test_video_pipeline.py
│   ├── test_video_service.py
│   └── story2video/           # Story2Video 单元测试
│       ├── test_audio_mixer.py
│       ├── test_pipeline.py
│       ├── test_slideshow.py
│       └── test_text_segmentation.py
│
├── scripts/
│   └── migrate_users.py       # 用户数据迁移脚本
│
├── docs/
│   └── PRD.md                 # 本文档
│
├── AGENTS.md                  # 开发规范指南
├── CLAUDE.md                  # Claude 工作指令
├── pyproject.toml             # Python 项目配置
└── orchestrator.db            # SQLite 数据库（本地开发）
```

### 4.3 路由规范

| 方法 | 路径 | 模块 | 鉴权 | 功能开关 | 说明 |
|------|------|------|------|---------|------|
| GET | `/health` | 系统 | 否 | - | 健康检查 |
| GET | `/api/features` | 系统 | 否 | - | 功能列表 |
| POST | `/auth/register` | Auth | 否 | - | 用户注册 |
| POST | `/auth/login` | Auth | 否 | - | 用户登录 |
| POST | `/auth/refresh` | Auth | 否 | - | Token 刷新 |
| GET | `/api/trending/*` | TrendScope | 可选 | trending_feed | 热榜数据 |
| POST | `/api/articles/fetch` | Content-Aggregator | 是 | article_manual_fetch | 采集文章 |
| POST | `/api/articles/*/split` | Splitter | 是 | split_single | 智能分句 |
| POST | `/api/prompts/optimize` | Prompt-Engine | 是 | prompt_optimize | 提示词优化 |
| POST | `/api/jobs/video` | Video | 是 | video_fixed_template | 视频合成 |
| POST | `/api/jobs/publish` | Multi-Publish | 是 | publish_single_platform | 发布内容 |
| GET | `/dashboard` | Dashboard | 是 | - | 运营看板 |
| GET | `/` | Web | 否 | - | 统一前端入口 |

### 4.4 模块集成接口规范

所有子模块通过 `pip install -e` 安装为 Python 库，orchestrator 通过 `import` 直接调用：

```python
# 示例：集成 Smart-Sentence-Splitter
from splitter import SmartSentenceSplitter, SplitResult

splitter = SmartSentenceSplitter()

@router.post("/api/articles/{id}/split")
@requires_feature("split_single")
async def split_article(id: str, db = Depends(get_db)):
    # 获取文章 → splitter.split(text) → 保存结果 → 返回
    ...
```

**集成约束：**
- 不修改被引用模块的任何代码（零侵入）
- 各模块保持独立 Git 仓库和独立部署能力
- 模块间通过 `shared-models` (Pydantic v2) 交换数据

---

## 五、当前状态

### 5.1 Phase 0（已部署）

当前处于 **Phase 0（骨架阶段）**，版本 v0.3.0，已部署到 4G 阿里云 ECS：

| 项目 | 状态 | 说明 |
|------|------|------|
| FastAPI 骨架 | ✅ | 路由、中间件、数据库初始化完成 |
| 统一认证 | ✅ | JWT 注册/登录/刷新完整实现 |
| 功能开关 | ✅ | 13 个开关全配置，装饰器绑定 |
| 子模块 SDK | ✅ | 6 个子模块可编辑安装，同进程调用 |
| 视频串行 | ✅ | FIFO 队列，严格单视频任务 |
| 统一前端 | ✅ | Next.js 应用，Nginx 代理到 :3000 |
| Nginx 反向代理 | ✅ | /api/* → :8000，/ → :3000 |
| systemd 保活 | ✅ | 自动重启，崩溃恢复 |
| 数据库 | ✅ | aiosqlite (WAL) + PostgreSQL (auth) |
| 测试套件 | ✅ | 25+ 测试，含 e2e 管道测试 |
| 内存监控 | ✅ | idle < 200MB，峰值 < 800MB |

### 5.2 部署架构

```
                              Internet
                                  │
                          ┌───────▼───────┐
                          │   Nginx :443  │
                          │  SSL 终止     │
                          └───┬───────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        ┌─────▼──────┐ ┌─────▼──────┐  ┌─────▼──────┐
        │ /api/*     │ │  /         │  │ /static    │
        │→:8000      │ │→:3000      │  │→:8000      │
        │ FastAPI    │ │ Next.js    │  │ 静态文件    │
        │ systemd    │ │ 前端       │  │            │
        └─────┬──────┘ └────────────┘  └────────────┘
              │
     ┌────────┴────────┐
     │ 4G Alibaba ECS │
     │ Alibaba Cloud  │
     │  Linux 3       │
     └─────────────────┘
```

### 5.3 数据管道（全链路）

```
TrendScope 热榜发现
    │  trending_to_pipeline (tier 2)
    ▼
Content-Aggregator 采集 + AI 改写
    │
    ▼
Smart-Sentence-Splitter 智能分句
    │
    ▼
Prompt-Engine 提示词优化
    │
    ▼
Story2Video 视频合成 (串行, FIFO队列)
    │
    ▼
Multi-Publish 多平台发布
```

---

## 六、版本历史和路线图

### 6.1 版本历史

| 版本 | 日期 | 内容 | 状态 |
|------|------|------|------|
| v0.1.0 | 2026-05 | 初始骨架搭建，基础路由 + JWT 认证 + SQLite | ✅ |
| v0.2.0 | 2026-05 | PostgreSQL 支持，auth 表 + 订阅 tier | ✅ |
| v0.3.0 | 2026-06 | Nginx 反向代理，统一前端整合，ECS 部署 | ✅ |

### 6.2 路线图

| 阶段 | 版本 | 内容 | 目标 |
|------|------|------|------|
| **Phase 0** | v0.3.x | 骨架 + 路由 + 认证 + 部署 + 基础 SDK 集成 | ✅ 已上线 |
| **Phase 1** | v0.4.x | BackgroundTasks 异步编排 + 全链路管道 + 进度推送 | 📅 Next |
| **Phase 2** | v0.5.x | 任务持久化（SQLite）+ 失败重试 + 回调通知 | 📅 规划中 |
| **Phase 3** | v0.6.x | 计费集成 + API 用量统计 + 管理员面板 | 📅 规划中 |
| **Phase 4** | v1.0.0 | 生产稳定版，全链路 SLA 保障 + 监控告警 | 📅 规划中 |

### 6.3 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 子模块 SDK 兼容性 | 高 | shared-models 统一数据契约 + 可编辑安装即时更新 |
| 内存超限 | 高 | 严格串行视频任务 + systemd MemoryMax=2.5G |
| 单点故障 | 中 | Nginx 反向代理 + systemd 自动重启 |
| 子模块独立变更破坏集成 | 中 | 回归测试套件 + CI 门禁 |
| PostgreSQL 连接池耗尽 | 中 | 异步驱动（asyncpg）+ 连接池限制 |

---

## 七、验收标准

### v0.3.0 验收（Phase 0）

- [x] `/health` 返回 `{"status": "ok"}`
- [x] JWT 注册 → 登录 → Token 刷新 → 鉴权保护完整流程
- [x] 功能开关加载 + `@requires_feature` 装饰器正常工作
- [x] 所有 6 个子模块 SDK 可成功导入
- [x] 视频任务严格串行执行（FIFO 队列验证通过）
- [x] Nginx 反向代理配置正确：`/api/*` → :8000，`/` → :3000
- [x] systemd 服务开机自启 + 崩溃自动重启
- [x] idle 内存 < 200MB
- [x] 25+ 测试全部通过
- [x] unified-frontend 正常可访问

### v0.4.0 目标（Phase 1）

- [ ] BackgroundTasks 全链路管道：trend → collect → rewrite → split → prompt → video → publish
- [ ] 任务进度推送（WebSocket 或 SSE）
- [ ] 任务状态查询 API
- [ ] 管道失败回滚或降级策略
- [ ] 各阶段耗时监控埋点

---

## 八、开发规范

详见 `AGENTS.md`，包含：
- 路由添加流程（`routers/` + `main.py` 注册）
- 功能开关添加（`feature_gates.yaml` + `@requires_feature`）
- 鉴权模式（公开端点 / 保护端点）
- 数据库操作（aiosqlite 依赖注入）
- 模块对接（Python SDK 导入方式）
- 测试规范（pytest + TestClient + 回归测试）
- 提交规范（feat/fix/docs/refactor）
- 资源守则（不引入新服务、不引入重框架、内存上限 2.5G、视频串行）
