#!/usr/bin/env bash
# =============================================================================
# start-dev.sh — 开发环境一键启动 orchestrator (SQLite 模式)
# =============================================================================
# 用法:
#   ./scripts/start-dev.sh            # 使用默认密钥
#   PO_SECRET_KEY="my-key" ./scripts/start-dev.sh  # 自定义密钥
#
# 前置条件:
#   pip install -e .            # 安装 orchestrator 依赖
#   pip install aiosqlite       # SQLite 异步驱动
#
# 说明:
#   - 使用 SQLite 替代 PostgreSQL，免去本地 PG 安装
#   - 端口 8000，热重载模式
#   - auth 路由可正常注册/登录
#   - PostgreSQL 相关功能（配额/订阅）不可用
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# ── 默认配置 ──────────────────────────────────────────────────────
PO_SECRET_KEY="${PO_SECRET_KEY:-dev-secret-key-change-me-in-production}"
PO_DATABASE_URL="${PO_DATABASE_URL:-sqlite+aiosqlite:///./orchestrator.db}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# ── 启动 ──────────────────────────────────────────────────────────
echo "🚀 Starting orchestrator (SQLite dev mode)..."
echo "   URL:  http://localhost:${PORT}/docs"
echo "   DB:   ${PO_DATABASE_URL}"
echo "   Exit: Ctrl+C"
echo ""

PO_SECRET_KEY="${PO_SECRET_KEY}" \
PO_DATABASE_URL="${PO_DATABASE_URL}" \
exec uvicorn main:app --reload --host "${HOST}" --port "${PORT}"
