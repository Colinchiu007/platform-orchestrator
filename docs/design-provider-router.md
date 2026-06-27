# ProviderRouter — 统一 LLM 配置管理与双层面 UI

> **设计日期**: 2026-06-27
> **状态**: ✅ Implemented (全部阶段完成)
> **涉及项目**: platform-orchestrator (后端 API + 服务) + unified-frontend (Admin/User 页面)

---

## Why

当前每增加一个 LLM 提供商，就需要在 `config.py` 加一个新 env var 字段，在 `systemd` 加一行 `Environment=`，在各个 service 文件里写 `settings.xxx_api_key`。随着支持的提供商增多，这不可持续。

**目标**: 一套统一的、可编程管理的 Provider 配置系统，带 Admin 运营后台 + 用户自配置界面。

---

## 架构

### 数据模型

```sql
-- Admin 配置的提供商
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,              -- UUID
    name TEXT UNIQUE NOT NULL,        -- "openai", "doubao", "minimax" 
    provider_type TEXT NOT NULL,      -- "llm" | "tts" | "image" | "video"
    display_name TEXT NOT NULL,       -- 展示名 "OpenAI GPT-4o"
    base_url TEXT NOT NULL,           -- API endpoint
    api_key_encrypted TEXT NOT NULL,  -- AES-GCM 加密存储
    models JSON DEFAULT '[]',        -- ["gpt-4o-mini", "gpt-4o"]
    config JSON DEFAULT '{}',         -- 额外参数（temperature, max_tokens, rate_limit...）
    enabled INTEGER DEFAULT 1,
    min_tier INTEGER DEFAULT 1,       -- 最小可用 tier
    created_at TEXT,
    updated_at TEXT
);

-- 用户自带的 API Key（覆盖 admin 配置）
CREATE TABLE user_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_uuid TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,   -- 用户自己的 key
    base_url TEXT,                     -- 可选覆盖
    is_active INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(user_uuid, provider_name)
);
```

### ProviderRouter 服务层

```python
class ProviderRouter:
    """Central provider configuration manager."""
    
    def get(self, name: str, user_uuid: str = None) -> ProviderConfig:
        """获取 provider 配置。如果用户有自配置 key，优先使用。"""
    
    def get_client(self, name: str, user_uuid: str = None) -> dict:
        """返回 {api_key, base_url, model} 给各 service 使用。"""
    
    def list_available(self, min_tier: int) -> list[dict]:
        """按 tier 列出可用 provider。"""
```

各 service 的改动极小：
- `rewrite.py`: `settings.openai_api_key` → `router.get("openai")["api_key"]`
- `tts_service.py`: `settings.doubao_api_key` → `router.get("doubao")["api_key"]`
- `image_service.py`: `settings.minimax_api_key` → `router.get("minimax")["api_key"]`

### API 路由

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| GET | `/api/admin/providers` | admin | 列出所有 provider 配置 |
| POST | `/api/admin/providers` | admin | 创建 provider |
| PUT | `/api/admin/providers/{name}` | admin | 更新 provider |
| DELETE | `/api/admin/providers/{name}` | admin | 删除 provider |
| POST | `/api/admin/providers/{name}/test` | admin | 测试连接 |
| GET | `/api/user/providers` | auth | 用户可见的 provider 列表 |
| GET | `/api/user/providers/{name}` | auth | 用户看单个 provider（不含管理员 key） |
| PUT | `/api/user/providers/{name}/key` | auth | 用户设置自己的 API key |
| DELETE | `/api/user/providers/{name}/key` | auth | 用户删除自设 key |

### 前端页面

- **Admin**: `/admin/providers` — 表格展示所有 provider，点击可编辑，新增/删除/测试连接
- **User**: `/settings/providers` — 展示用户 tier 可用的 provider 列表，可设置/删除自己的 API key

### 加密

API Key 在 DB 中以 AES-GCM 加密存储。使用 `PO_ENCRYPTION_KEY`（从 `PO_SECRET_KEY` 派生）作为加密密钥。

---

## 阶段划分

| Phase | 内容 | 依赖 | 状态 |
|-------|------|------|------|
| P1 | DB Schema + ProviderRouter 服务 | 无 | ✅ |
| P1b | 迁移现有 service 到 ProviderRouter | P1 | ✅ |
| P2 | Admin CRUD API + User provider API | P1 | ✅ |
| P3 | Adm