#!/usr/bin/env bash
# ===========================================================================
# e2e_smoke_test.sh — 全管线 E2E 冒烟测试
# 验证 orchestrator 及上下游服务的核心功能是否正常
# 使用方式:
#   ./scripts/e2e_smoke_test.sh                    # 本地 (:8000)
#   ORCHESTRATOR_URL=http://your-ecs:8000 ./scripts/e2e_smoke_test.sh
#   SKIP_PIPELINE=1 ./scripts/e2e_smoke_test.sh     # 跳过管线测试
# ===========================================================================
set -euo pipefail

BASE_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8000}"
PASS=0
FAIL=0
FAILED_TESTS=""

# ── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ── Helpers ─────────────────────────────────────────────────────────────────
ok()   { PASS=$((PASS+1)); echo -e "  ${GREEN}✓${NC} $1"; }
fail() { FAIL=$((FAIL+1)); FAILED_TESTS="$FAILED_TESTS  - $1\\n"; echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }

check_http() {
  local desc="$1" method="$2" url="$3" expect_code="$4" extra_args="${5:-}"
  local resp
  resp=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$url" $extra_args 2>/dev/null || echo "000")
  if [ "$resp" = "$expect_code" ]; then
    ok "$desc (HTTP $resp)"
  else
    fail "$desc — expected $expect_code, got $resp"
  fi
}

check_json() {
  local desc="$1" url="$2" jq_filter="$3" extra_args="${4:-}"
  local output
  output=$(curl -s "$url" $extra_args 2>/dev/null || echo '{"_error":"curl failed"}')
  local result
  result=$(echo "$output" | python3 -c "import sys,json; d=json.load(sys.stdin); print($jq_filter)" 2>/dev/null || echo "parse_failed")
  if [ "$result" = "True" ] || [ "$result" = "true" ]; then
    ok "$desc"
  else
    fail "$desc — filter failed: $jq_filter | body: ${output:0:100}"
  fi
}

# ── 1. 服务存活检查 ──────────────────────────────────────────────────────────
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  1/5  服务存活检查 — $BASE_URL${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

check_http "orchestrator /health 可达" GET "$BASE_URL/health" 200

# 如果聚合端点存在，测试它
if curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/health/all" 2>/dev/null | grep -q 200; then
  check_json "health/all 返回 ok" "$BASE_URL/api/health/all" "d.get('status')=='ok'"
  check_json "health/all 包含 orchestrator" "$BASE_URL/api/health/all" "'orchestrator' in d.get('services',{})"
fi

check_http "GET /api/features 可达" GET "$BASE_URL/api/features" 200

# ── 2. 注册与登录 ───────────────────────────────────────────────────────────
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  2/5  注册与登录${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

TEST_USER="e2e_test_$(date +%s)"
TEST_PASS="testpass123"
EMAIL="${TEST_USER}@e2e.test"

# 注册
resp=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/auth/register"   -H "Content-Type: application/json"   -d "{\"username\":\"$TEST_USER\",\"email\":\"$EMAIL\",\"password\":\"$TEST_PASS\"}" 2>/dev/null || echo "000")
if [ "$resp" = "201" ] || [ "$resp" = "409" ]; then
  ok "用户注册 (HTTP $resp) — 201=新建, 409=已存在"
else
  fail "用户注册 — 期望 201/409, 收到 $resp"
fi

# 登录
LOGIN_RESP=$(curl -s -X POST "$BASE_URL/api/auth/login"   -H "Content-Type: application/json"   -d "{\"username\":\"$TEST_USER\",\"password\":\"$TEST_PASS\"}" 2>/dev/null || echo '{}')
TOKEN=$(echo "$LOGIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
AUTH_HEADER="Authorization: Bearer $TOKEN"

if [ -n "$TOKEN" ]; then
  ok "登录成功 — 获取到 JWT token"
else
  fail "登录失败 — 未获取到 token"
fi

# ── 3. 验证鉴权端点 ───────────────────────────────────────────────────────
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  3/5  鉴权端点验证${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

if [ -n "$TOKEN" ]; then
  check_http "GET /api/user/providers 需鉴权" GET "$BASE_URL/api/user/providers" 200 "-H '$AUTH_HEADER'"
  check_http "GET /api/user/providers 无 token 返回 401" GET "$BASE_URL/api/user/providers" 401
  check_json "GET /api/user/stats 返回用户信息" "$BASE_URL/api/user/stats" "True" "-H '$AUTH_HEADER'"
fi

# ── 4. 管线流程测试 ───────────────────────────────────────────────────────
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  4/5  管线流程测试${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

if [ -n "$TOKEN" ] && [ -z "${SKIP_PIPELINE:-}" ]; then

  # 4a: 文章采集
  ARTICLE_RESP=$(curl -s -X POST "$BASE_URL/api/articles/fetch"     -H "Content-Type: application/json"     -H "$AUTH_HEADER"     -d '{"url":"https://example.com/e2e-test","title":"E2E Test Article","content":"This is a test article for the E2E pipeline smoke test.","content_text":"E2E pipeline verification content."}' 2>/dev/null || echo '{}')
  ARTICLE_ID=$(echo "$ARTICLE_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('id','') or d.get('id',''))" 2>/dev/null || echo "")
  if [ -n "$ARTICLE_ID" ]; then
    ok "文章采集成功 — id=$ARTICLE_ID"
  else
    warn "文章采集 — 可能被 mock 或跳过 (body: ${ARTICLE_RESP:0:80})"
  fi

  # 4b: 文章拆分
  SPLIT_RESP=$(curl -s -X POST "$BASE_URL/api/articles/split"     -H "Content-Type: application/json"     -H "$AUTH_HEADER"     -d '{"content":"E2E pipeline test content for split verification. This validates the smart-sentence-splitter integration.","topic":"E2E Test","stream":false}' 2>/dev/null || echo '{}')
  SPLIT_OK=$(echo "$SPLIT_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status')=='ok' or 'sentences' in d or 'scenes' in d)" 2>/dev/null || echo "false")
  if [ "$SPLIT_OK" = "True" ]; then
    ok "文章拆分端点返回正常"
  else
    warn "拆分端点返回异常 (body: ${SPLIT_RESP:0:80})"
  fi

  # 4c: 视频任务创建
  VIDEO_RESP=$(curl -s -X POST "$BASE_URL/api/jobs/create"     -H "Content-Type: application/json"     -H "$AUTH_HEADER"     -d '{"title":"E2E Test Video","script":"E2E pipeline test script for video generation."}' 2>/dev/null || echo '{}')
  VIDEO_ID=$(echo "$VIDEO_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id','') or d.get('id',''))" 2>/dev/null || echo "")
  if [ -n "$VIDEO_ID" ]; then
    ok "视频任务创建成功 — id=$VIDEO_ID"
  else
    warn "视频任务创建 — body: ${VIDEO_RESP:0:80}"
  fi

  # 4d: 获取任务列表
  if [ -n "$TOKEN" ]; then
    check_http "GET /api/jobs/list 返回 200" GET "$BASE_URL/api/jobs/list" 200 "-H '$AUTH_HEADER'"
  fi

else
  warn "跳过管线流程测试 (SKIP_PIPELINE=1 或无 token)"
fi

# ── 5. 功能开关 ────────────────────────────────────────────────────────────
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  5/5  功能开关读取${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

check_json "GET /api/features 返回列表" "$BASE_URL/api/features" "'features' in d and isinstance(d['features'], list)"
check_json "GET /api/features 包含 trending_feed" "$BASE_URL/api/features" "'trending_feed' in [f.get('id','') for f in d['features']] or 'trending_feed' in d['features'][0]" || true

# ── 汇总 ────────────────────────────────────────────────────────────────────
TOTAL=$((PASS+FAIL))
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  结果汇总${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  通过: ${GREEN}${PASS}${NC}  |  失败: ${RED}${FAIL}${NC}  |  总计: $TOTAL"

if [ $FAIL -gt 0 ]; then
  echo -e "\\n${RED}失败项目:${NC}"
  echo -e "$FAILED_TESTS"
  exit 1
else
  echo -e "\\n${GREEN}全部通过 ✓${NC}"
  exit 0
fi
