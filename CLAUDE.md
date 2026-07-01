# platform-orchestrator

薄壳统一入口，整合所有模块（FastAPI 8000）。

## 快速导航

完整开发规范见 [AGENTS.md](AGENTS.md)，包含：路由添加流程、功能开关、鉴权模式、数据库、模块对接方式、测试规范。

## 开发环境启动

```bash
# 前置条件
pip install aiosqlite

# 一键启动（SQLite 模式，免 PG）
./scripts/start-dev.sh

# 手动启动
PO_DATABASE_URL="sqlite+aiosqlite:///./orchestrator.db" \
PO_SECRET_KEY="dev-secret-key-change-me-in-production" \
uvicorn main:app --reload --port 8000
```

启动后访问 http://localhost:8000/docs 查看 API。

## 双通道数据库

| 通道 | 用途 | 引擎 |
|------|------|------|
| `db.py` | 业务表（articles/jobs/payments） | aiosqlite（本地文件） |
| `db_pg.py` | Auth 表（users/refresh_tokens） | 默认 PG → SQLite 降级 |

Auth 通道默认连 PostgreSQL（与 TrendScope 共享实例），本地开发通过 `PO_DATABASE_URL=sqlite+aiosqlite://` 自动降级。
