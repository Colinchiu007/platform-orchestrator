---
name: platform-orchestrator-design
description: platform-orchestrator DESIGN.md — 设计文档
---

# Platform Orchestrator — 设计文档

> **版本**: v0.5.2 | **关联**: docs/PRD.md, docs/ARCHITECTURE.md, AGENTS.md

---

## 一、设计目标

### 1.1 核心原则

| 原则 | 说明 |
|------|------|
| **薄壳原则** | orchestrator 只做路由+鉴权+编排，不放业务逻辑 |
| **零侵入** | 不修改任何被引用模块的代码 |
| **同进程运行** | 所有模块作为库导入，不引入多进程/微服务 |
| **零框架锁定** | 不使用 Django ORM/SQLAlchemy，用 aiosqlite |
| **功能开关驱动** | 所有功能通过 `feature_gates.yaml` 动态控制 |

### 1.2 设计取舍

| 取舍 | 选择 | 理由 |
|------|------|------|
| 同进程 vs 微服务 | **同进程 SDK 导入** | 零网络开销，适配 4G ECS |
| SQLite vs PostgreSQL | **两者都支持**（SQLite 本地开发 + PG 生产） | 开发便捷 + 生产可靠 |
| 硬编码 vs 功能开关 | **YAML 配置化开关** | 不改代码控制功能可用性 |
| 硬编码 API Key vs ProviderRouter | **Fernet 加密 DB 存储** | 可运营、可审计、零硬编码 |
| 线性管道 vs DAG 编排 | **DAG (Block 引擎)** | 可扩展、可并行、可局部重试 |

---

## 二、认证设计

### 2.1 JWT 双 Token 方案

| Token | 存储位置 | 有效期 | 用途 |
|-------|---------|--------|------|
| Access Token | 前端内存 | 2h | API 鉴权 |
| Refresh Token | 前端 localStorage | 30d | 续期 |

### 2.2 认证流程

```
注册 → bcrypt 加密密码 → 存入 users 表
登录 → 验证密码 → 签发 access + refresh token
请求 → Authorization: Bearer <access_token> → get_current_user()
刷新 → POST /auth/refresh → 验证 refresh token → 签发新 access token
```

### 2.3 多数据库策略

- 开发环境：SQLite（`orchestrator.db`，WAL 模式）
- 生产环境：PostgreSQL（auth/tier 表）+ SQLite（任务/队列表）
- ATTACH DATABASE 模拟多数据库关联

---

## 三、ProviderRouter 设计

### 3.1 为什么需要

原本各 service 文件硬编码 `settings.xxx_api_key`，新增 provider 需要改代码。ProviderRouter 用 DB 存储替代硬编码。

### 3.2 加密方案

```
PO_SECRET_KEY → SHA-256 → Fernet key (AES-GCM 128-bit)
    │
    ├── 写入: Fernet.encrypt(api_key.encode()) → 存储密文
    └── 读取: Fernet.decrypt(ciphertext) → 还原明文
```

### 3.3 双层面配置

| 层面 | 谁配置 | 存储表 | 覆盖关系 |
|------|--------|--------|---------|
| Admin | 运营后台 | provider_configs | 全局默认 |
| 用户 | 个人设置 | user_api_keys | 覆盖 Admin 对应 provider |

### 3.4 已迁移的服务

| 服务 | 原硬编码 | 现读取路径 |
|------|---------|----------|
| rewrite.py | `settings.openai_api_key` | `ProviderRouter.get("openai")` |
| tts_service.py | `settings.doubao_api_key` | `ProviderRouter.get("doubao")` |
| image_service.py | `settings.minimax_api_key` | `ProviderRouter.get("minimax")` |
| video_service.py | `settings.kling_api_key` | `ProviderRouter.get("kling")` |
| publish_service.py | `settings.wechat_appid` | `ProviderRouter.get("wechat")` |

---

## 四、功能开关设计

### 4.1 feature_gates.yaml 格式

```yaml
gates:
  trending_feed:
    tier: 1
    enabled: true
    description: "热榜展示（免费功能）"
  video_full_pipeline:
    tier: 2
    enabled: true
    description: "全流水线视频"
```

### 4.2 分级付费映射

| Tier | 订阅等级 | 可用功能示例 |
|------|---------|-------------|
| 1 | 免费用户 | trending_feed, article_manual_fetch, split_single |
| 2 | 基础版 | video_fixed_template, prompt_optimize |
| 3 | 专业版 | publish_multi_platform |
| 4 | 企业版 | 全部功能 |

### 4.3 @requires_feature 装饰器

```python
@router.post("/api/jobs/video")
@requires_feature("video_full_pipeline")
async def create_video(...):
    ...
```

- 未启用 → 返回 403 + 功能名称
- Tier 不足 → 返回 403 + 升级引导

---

## 五、并发控制设计

### 5.1 视频串行队列

```
ConcurrencyController
├── queue: asyncio.Queue (maxsize=5)
├── active: int (当前执行任务数, max=1)
│
├── register(task) → 加入队列
├── acquire() → 等待轮到自己
├── release() → 下一个任务开始
├── drain() → 拒绝新任务（shutdown 模式）
└── reset() → 清空队列（紧急恢复）
```

### 5.2 优雅关闭

AppLifecycle 管理 shutdown 流程：

```
收到 SIGTERM
    │
    ├── Phase 1: drain callbacks（停止接收新任务）
    │   ├── concurrency_control.drain()
    │   └── 其他 on_drain 回调
    │
    ├── Phase 2: cancel tasks（等待当前任务完成）
    │   └── wait_for(tasks, timeout=30s)
    │
    ├── Phase 3: force exit（二次 SIGTERM）
    │   └── os._exit(130)
    │
    └── cleanup → yield（lifespan 恢复）
```

---

## 六、测试策略

| 层级 | 方法 | 覆盖 |
|------|------|------|
| 单元测试 | pytest + fastapi.testclient | 70+ 用例 |
| E2E 测试 | TestClient + mock DB | 15 用例（auth/provider CRUD/usage）|
| Block 引擎 | pytest + asyncio | 15 用例 |
| 视频服务 | pytest + mock | 20+ 用例 |
| 功能开关 | pytest + mock | 8 用例 |
| 速率限制 | monkeypatch + 测试 | 5 用例 |
