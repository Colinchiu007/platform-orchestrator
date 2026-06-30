# PROJECT-000：platform-orchestrator — 统一入口薄壳 — PRD

> **立项日期**: 2026-06-03
> **最后更新**: 2026-06-27
> **当前版本**: v0.5.2（Phase 0 已部署，Phase 1 全管线就绪 — pipeline_v2 启用 + ProviderRouter 全栈 + Membership Phase 2 完成）
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
6. **统一 LLM 配置**：ProviderRouter 中心化管理所有 AI 提供商的 API Key、Base URL、模型映射，支持 Admin 运营后台配置 + 用户自配置覆盖
7. **资源可控**：严格的内存和并发约束，适配 4G 阿里云 ECS

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
| ✅ 统一 LLM 配置 | ProviderRouter 中心化管理 7+ AI 提供商的 API Key，加密存储，双层面 UI（Admin + 用户） |
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

#### F2.1：用量跟踪 (Usage Tracking)

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 日用量表 | `daily_usage` 表记录用户每日视频配额消耗，包含 `user_uuid`、`date`、`videos_used`、`videos_quota` | ✅ |
| 配额计算 | 免费用户日配额 2 条，付费用户按订阅等级递增（Basic 10 / Pro 50 / Enterprise 无限） | ✅ |
| 原子递减 | `POST /api/subscription/consume` 原子递减当日配额（SQL 级 `UPDATE ... SET videos_used = videos_used + 1 WHERE ...`） | ✅ |
| 用量查询 | `GET /api/subscription/usage` 返回当日用量和配额 | ✅ |
| 前端展示 | 设置页「订阅用量」卡片展示配额环 + 剩余条数 + 套餐名称 | ✅ |
| 配额拦截 | 视频创建时自动检测配额，不足时返回 429 + 明确错误提示 | ✅ |
| 升级入口 | 配额不足时前端显示「升级套餐」引导按钮，跳转支付页 | ✅ |

#### F2.2：会员周期管理 (Subscription Lifecycle)

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 过期检测 | `services/subscription_lifecycle.py` — `check_expired_subscriptions(db)` 扫描 `subscriptions` 表，检测 `end_date < now() AND status = 'active'` 的记录 | ✅ |
| 自动过期 | 过期订阅状态自动标记为 `expired`，用户 `subscription_type` 降级为 `free` | ✅ |
| 启动维护 | `daily_maintenance()` 在应用启动时自动执行（lifespan hook），使用独立 aiosqlite 连接 | ✅ |
| 保护规则 | NULL end_date（终身订阅）和 future end_date 不受影响；已过期订阅不重复处理 | ✅ |
| 测试覆盖 | 8 项测试覆盖过期/Future/NULL/已过期/混合场景，全部通过 | ✅ |

#### F2.3：管理员用户管理 (Admin User Management)

| 子功能 | 描述 | 状态 |
|--------|------|------|
| 用户列表 | `GET /api/admin/users` — 分页列出用户，支持 `subscription_type`/`is_active` 过滤 + `search` 搜索 | ✅ |
| 用户详情 | `GET /api/admin/users/{uuid}` — 获取用户基本信息、订阅信息、近30天用量历史 | ✅ |
| 状态切换 | `PUT /api/admin/users/{uuid}/status` — 管理员激活/停用用户 | ✅ |
| 鉴权保护 | 所有 admin 端点要求 `role=admin`，非 admin 返回 403 | ✅ |
| 前端页面 | `/admin/users` — 表格展示用户列表，含搜索、套餐过滤、状态切换、分页、详情弹窗 | ✅ |
| 16 项测试 | TDD 驱动，覆盖列表/分页/过滤/搜索/权限/详情/状态切换，全部通过 | ✅ |

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
| `video_full_pipeline` | 2 | true | 全流水线视频（已开放） |
| `video_concurrency_control` | 1 | true | 视频串行控制 |
| `publish_single_platform` | 2 | true | 单平台发布 |
| `publish_multi_platform` | 3 | true | 多平台发布（已开放） |

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
| 内容管道 | 趋势发现 → 采集 → 改写 → 分句 → 提示词 → 视频的全流程编排 | ✅ Phase 1 (pipeline_v2) |

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
│   ├── provider_admin.py      # ProviderRouter Admin CRUD
│   ├── provider_user.py       # 用户自配置 API 路由
│   ├── publish.py             # 多平台发布路由
│   ├── splitter.py            # 智能分句路由
│   ├── trending.py            # 热榜路由
│   ├── video.py               # 视频合成路由
│   └── web.py                 # 前端页面路由
│
├── engine/                    # Block 编排引擎 (v0.4.x+)
│   ├── __init__.py            # 包初始化 + Block 注册表
│   ├── block.py               # Block 基类 (ABC + 泛型 + AsyncGenerator)
│   ├── graph.py               # Graph/Node/Link 数据模型 + DAG 验证
│   ├── executor.py            # DAG 执行引擎 + 状态机
│   └── errors.py              # 异常类型体系
│
├── blocks/                    # 示例 Block 实现 (v0.4.x+)
│   ├── __init__.py            # 自动注册所有 Block
│   ├── splitter_block.py      # 分句 Block
│   ├── optimizer_block.py     # 提示词优化 Block
│   ├── tts_block.py           # 语音合成 Block
│   ├── image_gen_block.py     # 图片生成 Block
│   └── compose_block.py       # 视频合成 Block
│
├── services/                  # 业务服务层
│   ├── collect.py             # 内容采集服务
│   ├── compositor.py          # 合成引擎
│   ├── concurrency_control.py # 并发控制（视频串行）
│   ├── image_service.py       # 图片处理服务
│   ├── pipeline.py            # 全流程编排引擎
│   ├── prompt_service.py      # 提示词服务
│   ├── provider_router.py     # 统一 LLM 配置管理（Fernet 加密）
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
│   ├── test_engine.py         # Block 引擎单元测试 (v0.4.x+)
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
│   ├── PRD.md                 # 本文档
│   └── architecture-v2.md     # Block 编排架构补充说明 (v0.4.x+)
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
| GET | `/dashboard` | Dashboard | 是 | - | 运营看板 |
| GET | `/api/admin/providers` | ProviderRouter Admin | 是 (admin) | - | 列出所有 Provider |
| POST | `/api/admin/providers` | ProviderRouter Admin | 是 (admin) | - | 创建 Provider |
| PUT | `/api/admin/providers/{name}` | ProviderRouter Admin | 是 (admin) | - | 更新 Provider |
| DELETE | `/api/admin/providers/{name}` | ProviderRouter Admin | 是 (admin) | - | 删除 Provider |
| POST | `/api/admin/providers/{name}/test` | ProviderRouter Admin | 是 (admin) | - | 测试 Provider |
| GET | `/api/user/providers` | ProviderRouter User | 是 | - | 用户可见的 Provider 列表 |
| GET | `/api/user/providers/{name}` | ProviderRouter User | 是 | - | 查看单个 Provider |
| PUT | `/api/user/providers/{name}/key` | ProviderRouter User | 是 | - | 设置用户 API Key |
| DELETE | `/api/user/providers/{name}/key` | ProviderRouter User | 是 | - | 删除用户 API Key |
| GET | `/api/admin/users` | Admin Users | 是 (admin) | - | 列出用户（分页/过滤/搜索） |
| GET | `/api/admin/users/{uuid}` | Admin Users | 是 (admin) | - | 用户详情（含订阅+用量） |
| PUT | `/api/admin/users/{uuid}/status` | Admin Users | 是 (admin) | - | 激活/停用用户 |
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

### 4.5 ProviderRouter — 统一 LLM 配置管理

ProviderRouter 取代了原本分散在各 service 文件中的 `settings.xxx_api_key` 硬编码模式。

**数据模型：**

```sql
-- Admin 配置的提供商
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,              -- UUID
    name TEXT UNIQUE NOT NULL,        -- "openai", "doubao", "minimax"
    provider_type TEXT NOT NULL,      -- "llm" | "tts" | "image" | "video"
    display_name TEXT NOT NULL,       -- 展示名
    base_url TEXT NOT NULL,           -- API endpoint
    api_key_encrypted TEXT NOT NULL,  -- Fernet (AES-GCM) 加密存储
    models JSON DEFAULT '[]',
    config JSON DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    min_tier INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

-- 用户自带的 API Key（覆盖 admin 配置）
CREATE TABLE user_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_uuid TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    base_url TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(user_uuid, provider_name)
);
```

**加密方案：**
- 使用 `cryptography.fernet.Fernet`（AES-GCM 128-bit）
- 加密密钥从 `PO_SECRET_KEY` 通过 SHA-256 派生
- 用户自配置 Key 和管理员 Key 分表存储，加密方式相同

**双层面 UI：**
- **Admin 运营后台** `[admin]`：`/admin/providers` — 表格展示所有 Provider，支持新增/编辑/删除/测试连接，配置所有提供商的 API Key、Base URL、模型、Tier 权限
- **用户自助** `[auth]`：`/settings/providers` — 展示用户 Tier 可用 Provider 列表，支持用户设置/删除自己的 API Key 覆盖管理员配置
- **路由**：后端 API 在 `routers/provider_admin.py` + `provider_user.py`；前端页面在 `unified-frontend/src/app/admin/providers/` + `src/app/settings/providers/`

**前端实现：**
- Admin 页面使用共享的 `AppLayout` 组件，侧边栏新增「Provider 管理」导航项
- Admin 页面支持三种状态：loading（骨架屏）、error（ErrorState + 重试）、empty（EmptyState + 引导按钮）
- Admin 页面内联编辑（点击行直接展开编辑表单），删除确认使用 `window.confirm`
- 用户页面使用折叠卡片布局（展开后配置 API Key），密码输入框支持明/暗文切换

**已迁移的服务：**

| `services/rewrite.py` | `settings.openai_api_key` / `openai_base_url` / `openai_model` | `get_router().get("openai")` |
| `services/tts_service.py` | `settings.doubao_api_key` | `get_router().get("doubao")` |
| `services/image_service.py` | `settings.minimax_api_key` / `settings.sensenova_api_key` / `settings.kling_api_key` | `get_router().get("minimax")` / `get_router().get("sensenova")` / `get_router().get("kling")` |
| `services/video_service.py` | `settings.kling_api_key` / `settings.jimeng_api_key` | `get_router().get("kling")` / `get_router().get("jimeng")` |
| `services/publish_service.py` | `settings.wechat_appid` / `settings.wechat_appsecret` | `get_router().get("wechat")` |

---

## 五、测试覆盖

### 5.1 测试框架与模式

| 维度 | 说明 |
|------|------|
| 框架 | pytest + fastapi.testclient.TestClient |
| 数据库 | SQLite（WAL模式）替代PostgreSQL，通过 conftest.py 的 ATTACH DATABASE 模拟 auth schema |
| 异步模式 | `asyncio_mode = strict` |
| 认证 | JWT 手动构造（admin token）或 register + login 流程 |
| 速率限制 | conftest.py 模块级 monkeypatch 替换 `rate_limit_video` 为 `"1000/hour"` |
| 测试隔离 | 每个测试函数前执行 `clean_tables` fixture，清除 provider_configs、user_api_keys、users、refresh_tokens、subscriptions |

### 5.2 E2E 集成测试 (test_pipeline_e2e.py)

测试文件：`tests/test_pipeline_e2e.py`（15 个测试用例）

| 测试类 | 测试方法 | 覆盖内容 |
|--------|---------|---------|
| **TestBasicEndpoints** | `test_health_check` | GET /health → 200, {"status":"ok"} |
| | `test_feature_gates` | GET /api/features → 200, features dict |
| **TestAuth** | `test_register_user` | POST /api/auth/register → 201, user created |
| | `test_login_returns_jwt` | POST /api/auth/login → 200, access+refresh token |
| **TestProviderAdminCRUD** | `test_create_provider` | POST /api/admin/providers → 201 (admin JWT) |
| | `test_list_providers` | GET /api/admin/providers → list |
| | `test_update_provider` | PUT /api/admin/providers/{name} → update fields |
| | `test_delete_provider` | DELETE /api/admin/providers/{name} → 204 |
| | `test_full_crud_cycle` | Create → List → Update → Delete → Verify deletion |
| | `test_admin_rejects_non_admin` | Non-admin user → 403 Forbidden |
| **TestUserProviderOperations** | `test_list_available_providers` | GET /api/user/providers → tier-filtered list |
| | `test_set_and_view_provider_key` | PUT /api/user/providers/{name}/key → set + masked view |
| | `test_delete_user_key` | DELETE /api/user/providers/{name}/key → 204 |
| **TestUsageTracking** | `test_usage_requires_auth` | GET /api/user/usage → 401 without auth |
| | `test_usage_returns_daily_info` | GET /api/user/usage → 200 with quota info |

### 5.3 测试统计

| 指标 | 数值 |
|------|------|
| 测试文件总数 | 20+ |
| 总测试用例数 | 70+ |
| E2E 测试数 | 15 |
| ProviderRouter 单元测试 | 17 |
| 引擎 (Block/Graph/Executor) 测试 | 15 |
| 视频服务单元测试 | 20+ |
| 功能开关测试 | 8 |

### 5.4 运行方式

```bash
# 运行全部测试
python -m pytest tests/ -v

# 运行 E2E 测试
python -m pytest tests/test_pipeline_e2e.py -v

# 运行 ProviderRouter 单元测试
python -m pytest tests/test_provider_router.py -v

# 运行引擎测试
python -m pytest tests/test_engine.py -v
```

### 5.5 已知问题

| 问题 | 影响范围 | 根因 |
|------|---------|------|
| `routers/video.py` 缺少 `increment_usage` / `QuotaExceededError` 导入 | video pipeline e2e 测试 | 引用 `services.quota` 但未 import |
| 部分 auth 测试直接查询 `orchestrator.db` 而非 `test_auth.db` | auth/login e2e 测试 | PG→SQLite 迁移后测试未更新 |
| feature_gates.yaml 在 CI 环境不存在 | features 端点 e2e 测试 | 测试环境需配置 feature_gates.yaml 路径 |
