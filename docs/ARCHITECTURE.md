---
name: platform-orchestrator-architecture
description: platform-orchestrator ARCHITECTURE.md — 架构文档
---

# Platform Orchestrator — 架构文档

> **版本**: v0.5.2 | **更新**: 2026-07-01
> **定位**: 一站式视频生成平台的薄壳统一入口

---

## 一、系统架构总览

```
                          用户 (Browser / API Client)
                                    │
                              ┌─────▼─────┐
                              │   Nginx    │  :80 / :443
                              │  反向代理   │
                              └─────┬─────┘
                         ┌──────────┼──────────┐
                         │          │          │
                    ┌────▼────┐ ┌───▼────┐ ┌───▼────┐
                    │ /api/*  │ │   /    │ │ /static│
                    │ →:8000  │ │→:3000  │ │ 静态   │
                    └────┬────┘ │ (前端)  │ └────────┘
                         │      └────────┘
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI (port 8000)                            │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   Middleware                              │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │    │
│  │  │ CORS     │  │  Auth    │  │Rate Limit│  │Feature  │ │    │
│  │  │          │  │ JWT 鉴权 │  │ 令牌桶    │  │Gate     │ │    │
│  │  └──────────┘  └──────────┘  └──────────┘  └─────────┘ │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                     Routers                               │    │
│  │  ┌──────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌────────┐   │    │
│  │  │ Auth │ │Trend │ │Articles│ │ Split  │ │ Prompt │   │    │
│  │  │/auth │ │/trend│ │ /api   │ │ /split │ │ /prompt│   │    │
│  │  └──────┘ └──────┘ └────────┘ └────────┘ └────────┘   │    │
│  │  ┌──────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌────────┐   │    │
│  │  │ Video│ │Publish│ │ Payment │ │Admin   │ │Dashboard│   │    │
│  │  │/jobs │ │ /jobs │ │ /pay   │ │ /admin │ │ /dash   │   │    │
│  │  └──────┘ └──────┘ └────────┘ └────────┘ └────────┘   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   Services                                │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │    │
│  │  │ Pipeline │ │Compositor│ │ Rewrite  │ │Concurrency│   │    │
│  │  │ 编排引擎  │ │ 合成引擎  │ │ 改写引擎  │ │ 并发控制  │   │    │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │    │
│  │  │ Image    │ │ TTS      │ │ Provider │ │Publish   │   │    │
│  │  │ 图片服务  │ │ 语音服务  │ │ Router   │ │ 发布服务  │   │    │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │               Block 编排引擎 (engine/)                    │    │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────────┐ │    │
│  │  │ Block   │ │ Graph   │ │Executor │ │ blocks/      │ │    │
│  │  │ 基类    │ │ 图模型   │ │ 执行引擎 │ │ 5 具体 Block │ │    │
│  │  └─────────┘ └─────────┘ └─────────┘ └──────────────┘ │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Python SDK Imports (pip install -e)          │    │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌───────┐ │    │
│  │  │Trend-  │ │Content-│ │ SSS    │ │Prompt  │ │Story2 │ │    │
│  │  │Scope   │ │Aggregat│ │分句器   │ │Engine  │ │Video  │ │    │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └───────┘ │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   Data Layer                              │    │
│  │  ┌──────────────────────┐  ┌─────────────────────────┐  │    │
│  │  │ aiosqlite (WAL 模式)  │  │ PostgreSQL 15          │  │    │
│  │  │ orchestrator.db      │  │ auth/tier 共享表        │  │    │
│  │  └──────────────────────┘  └─────────────────────────┘  │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、模块集成架构

### 2.1 子模块 SDK 导入

所有子模块通过 `pip install -e` 安装为可编辑包，同进程调用：

| 子模块 | Python 包 | 导入方式 | 路由前缀 |
|--------|----------|---------|---------|
| TrendScope | `trendscope` | `from trendscope.api.services import ...` | `/api/trending` |
| Content-Aggregator | `content_aggregator` | `from content_aggregator.services import ...` | `/api/articles` |
| Smart-Sentence-Splitter | `splitter` | `from splitter import SmartSentenceSplitter` | `/api/articles/*/split` |
| Prompt-Engine | `prompt_engine` | `from prompt_engine import Optimizer` | `/api/prompts` |
| Story2Video | Story2Video 前端 | 前端 API 调用 | `/api/jobs/video` |
| Multi-Publish | Multi-Publish 桌面端 | 前端 API 调用 | `/api/jobs/publish` |

**关键约束**：orchestrator 不修改被引用模块代码（零侵入）。

### 2.2 Block 编排引擎

引入 DAG 编排层替代硬编码的 5 步顺序管道：

```
传统管道:   split → optimize → TTS → image → compose（全部串行）

Block 编排:  split → optimize → TTS ──┐
                                   └→ image ──→ compose（TTS 和 Image 可并行）
```

**Phase 状态**：
- ✅ Phase 1 (v0.4.x)：Block 基建（基类/图模型/执行引擎/5 示例 Block）
- 🚧 Phase 2 (v0.5.x)：任务持久化 + 失败重试
- 📅 Phase 3 (v1.0.x)：可视化编排 + Block 市场

---

## 三、数据流：全管线视频生成

```
用户 POST /api/jobs/video
    │
    ├── auth: JWT 验证（get_current_user）
    ├── feature: video_full_pipeline 门禁
    ├── quota: 原子递减 daily_usage
    └── concurrency: 串行队列（FIFO）
    │
    ▼
Pipeline (pipeline.py):
    1. content-aggregator 采集/接收原文
    2. SSS 分句（SplitterBlock）
    3. Prompt-Engine 优化提示词（OptimizerBlock）
    4. TTS 语音合成（TTSBlock）
    5. 图片生成（ImageGenBlock）
    6. Compose 视频合成（ComposeBlock）
    │
    ▼
发布 POST /api/jobs/publish
    ├── 本地 RPA（B站/抖音）
    └── 云端 API（小红书/视频号/YouTube）
```

---

## 四、目录结构

```
platform-orchestrator/
├── main.py                      # FastAPI 入口 + 路由注册 + lifespan
├── config.py                    # 配置（pydantic-settings, PO_ 前缀）
├── db.py                        # aiosqlite 初始化（WAL 模式）
├── db_pg.py                     # PostgreSQL 初始化
├── middleware/                   # auth.py / feature_gate.py / rate_limit.py
├── routers/                     # 12+ 路由模块
├── services/                    # 业务服务层
├── engine/                      # Block 编排引擎（block.py / graph.py / executor.py）
├── blocks/                      # 5 个具体 Block 实现
├── models/                      # 数据模型
├── tests/                       # 70+ 测试用例
├── docs/                        # PRD.md / architecture-v2.md
├── AGENTS.md                    # 开发流程规范
└── CHANGELOG.md
```

---

## 五、部署架构

```
4G 阿里云 ECS (Alibaba Cloud Linux 3)
├── Nginx: 80/443 反向代理
├── FastAPI: 8000 (orchestrator)
│   └── SQLite: orchestrator.db (WAL 模式)
├── PostgreSQL: 5432 (auth/tier/trendscope 共享)
├── Next.js: 3000 (unified-frontend)
└── systemd: 所有服务进程保活 + 自动重启

资源约束:
├── 常驻内存 < 200MB（idle）
├── 峰值内存 < 800MB（视频任务）
└── 视频任务严格串行（1 个）
```
