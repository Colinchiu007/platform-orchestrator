#!/usr/bin/env bash
# ===========================================================================
# ecs-monitor.sh — ECS 生产监控脚本
# 定时运行 (推荐 crontab: */5 * * * *)，检查和告警服务状态
# 使用方式:
#   ./scripts/ecs-monitor.sh                      # 默认模式，输出到 stdout
#   SLACK_WEBHOOK_URL=https://hooks.slack.com/... ./scripts/ecs-monitor.sh   # 输出+Slack 告警
#   PROM_MODE=1 ./scripts/ecs-monitor.sh           # Prometheus 文本格式输出
# ===========================================================================
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────────────────
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8000}"
TRENDSCOPE_URL="${TRENDSCOPE_URL:-http://127.0.0.1:8001}"
SSS_URL="${SSS_URL:-http://127.0.0.1:8002}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:3000}"

DISK_WARN_PCT=80       # 磁盘使用率告警阈值
DISK_CRIT_PCT=90       # 磁盘使用率严重告警阈值
LOG_DIR="${LOG_DIR:-/var/log/orchestrator}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
BACKUP_MAX_AGE_HOURS=28  # 备份文件最大允许时间（小时）

SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
PROM_MODE="${PROM_MODE:-0}"

# ── 状态追踪 ──────────────────────────────────────────────────────────────
ALERTS=()
WARNINGS=()
PASS=0
FAIL=0

# ── 颜色 (stdout 模式) ─────────────────────────────────────────────────────
if [ "$PROM_MODE" = "0" ]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; CYAN=''; NC=''
fi

# ── Helpers ─────────────────────────────────────────────────────────────────
ok()    { PASS=$((PASS+1)); echo -e "  ${GREEN}✓${NC} $1"; }
fail()  { FAIL=$((FAIL+1)); echo -e "  ${RED}✗${NC} $1"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $1"; WARNINGS+=("$1"); }

alert() {
  local severity="$1" msg="$2"
  ALERTS+=("[$severity] $msg")
  if [ "$severity" = "CRIT" ]; then
    fail "$msg"
  else
    warn "$msg"
  fi
}

send_slack_alert() {
  [ -z "$SLACK_WEBHOOK_URL" ] && return 0
  local color="${1:-warning}" pretext="$2" text="$3"
  curl -s -X POST "$SLACK_WEBHOOK_URL"     -H "Content-Type: application/json"     -d "{\"attachments\":[{\"color\":\"$color\",\"pretext\":\"$pretext\",\"text\":\"$(echo "$text" | sed 's/"/\\"/g')\",\"ts\":$(date +%s)}]}"     -o /dev/null 2>/dev/null || true
}

# ── 1. 系统指标 ────────────────────────────────────────────────────────────
if [ "$PROM_MODE" = "1" ]; then
  # Prometheus 文本格式
  echo "# HELP ecs_node_cpu_usage CPU usage percentage"
  echo "# TYPE ecs_node_cpu_usage gauge"
  CPU_USAGE=$(top -bn1 2>/dev/null | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1 || echo "0")
  echo "ecs_node_cpu_usage $CPU_USAGE"

  echo "# HELP ecs_node_memory_usage_pct Memory usage percentage"
  echo "# TYPE ecs_node_memory_usage_pct gauge"
  MEM_USAGE=$(free | grep Mem | awk '{printf "%.1f", $3/$2 * 100}' 2>/dev/null || echo "0")
  echo "ecs_node_memory_usage_pct $MEM_USAGE"

  echo "# HELP ecs_node_disk_usage_pct Disk usage percentage"
  echo "# TYPE ecs_node_disk_usage_pct gauge"
  DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | tr -d '%' 2>/dev/null || echo "0")
  echo "ecs_node_disk_usage_pct $DISK_USAGE"

  echo "# HELP ecs_service_up Service probe result (1=up, 0=down)"
  echo "# TYPE ecs_service_up gauge"
  for svc in orchestrator trendscope sss frontend; do
    local url_var="${svc}_URL"
    local url="${!url_var}"
    local status=$(curl -s -o /dev/null -w "%{http_code}" "$url/health" 2>/dev/null || echo "000")
    echo "ecs_service_up{service=\"$svc\"} $([ "$status" = "200" ] && echo 1 || echo 0)"
  done
  exit 0
fi

echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  ECS 生产监控 — $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}\\n"

# ── 2. 服务健康检查 ───────────────────────────────────────────────────────
echo -e "${CYAN}── 服务健康检查 ──${NC}\\n"

check_service() {
  local name="$1" url="$2"
  local resp
  resp=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url/health" 2>/dev/null || echo "000")
  if [ "$resp" = "200" ]; then
    ok "$name 正常"
  else
    alert "CRIT" "$name 不可达 (HTTP $resp)"
  fi
}

check_service "orchestrator" "$ORCHESTRATOR_URL"
check_service "trendscope" "$TRENDSCOPE_URL"
check_service "SSS" "$SSS_URL"
check_service "frontend" "$FRONTEND_URL"

# ── 3. 磁盘使用率 ──────────────────────────────────────────────────────────
echo -e "\\n${CYAN}── 磁盘使用率 ──${NC}\\n"

DISK_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%' 2>/dev/null || echo "0")
echo -e "  磁盘使用率: ${DISK_PCT}%"

if [ "$DISK_PCT" -ge "$DISK_CRIT_PCT" ]; then
  alert "CRIT" "磁盘使用率 ${DISK_PCT}% 超过严重阈值 ${DISK_CRIT_PCT}%"
elif [ "$DISK_PCT" -ge "$DISK_WARN_PCT" ]; then
  alert "WARN" "磁盘使用率 ${DISK_PCT}% 超过预警阈值 ${DISK_WARN_PCT}%"
else
  ok "磁盘使用率 ${DISK_PCT}%"
fi

# ── 4. 日志文件检查 ────────────────────────────────────────────────────────
echo -e "\\n${CYAN}── 日志文件检查 ──${NC}\\n"

if [ -d "$LOG_DIR" ]; then
  LOG_SIZE=$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1 || echo "?")
  LOG_SIZE_MB=$(du -sm "$LOG_DIR" 2>/dev/null | cut -f1 || echo "0")
  echo -e "  日志目录: $LOG_DIR (${LOG_SIZE})"
  if [ "$LOG_SIZE_MB" -gt 500 ]; then
    alert "WARN" "日志目录 ${LOG_DIR} 超过 500MB (当前 ${LOG_SIZE})"
  else
    ok "日志大小正常"
  fi
else
  warn "日志目录 $LOG_DIR 不存在"
fi

# ── 5. 数据库备份检查 ──────────────────────────────────────────────────────
echo -e "\\n${CYAN}── 数据库备份检查 ──${NC}\\n"

if [ -d "$BACKUP_DIR" ]; then
  LATEST_BACKUP=$(find "$BACKUP_DIR" -name "*.sqlite" -o -name "*.sql" -o -name "*.dump" 2>/dev/null | sort -r | head -1 || echo "")
  if [ -n "$LATEST_BACKUP" ]; then
    BACKUP_TIME=$(stat -c '%Y' "$LATEST_BACKUP" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    AGE_HOURS=$(( (NOW - BACKUP_TIME) / 3600 ))
    echo -e "  最新备份: $(basename "$LATEST_BACKUP") (${AGE_HOURS}h 前)"
    if [ "$AGE_HOURS" -gt "$BACKUP_MAX_AGE_HOURS" ]; then
      alert "CRIT" "数据库备份超过 ${BACKUP_MAX_AGE_HOURS}h 未更新 (当前 ${AGE_HOURS}h)"
    else
      ok "数据库备份在有效期内"
    fi
  else
    alert "CRIT" "备份目录 $BACKUP_DIR 中未找到备份文件"
  fi
else
  warn "备份目录 $BACKUP_DIR 不存在"
fi

# ── 6. 内存使用率 ──────────────────────────────────────────────────────────
echo -e "\\n${CYAN}── 内存使用率 ──${NC}\\n"

MEM_INFO=$(free -m 2>/dev/null | grep Mem || echo "")
if [ -n "$MEM_INFO" ]; then
  MEM_TOTAL=$(echo "$MEM_INFO" | awk '{print $2}')
  MEM_USED=$(echo "$MEM_INFO" | awk '{print $3}')
  MEM_PCT=$(echo "$MEM_INFO" | awk '{printf "%.1f", $3/$2 * 100}')
  echo -e "  内存: ${MEM_USED}MB / ${MEM_TOTAL}MB (${MEM_PCT}%)"
  MEM_PCT_INT=$(echo "$MEM_PCT" | cut -d'.' -f1)
  if [ "$MEM_PCT_INT" -gt 90 ]; then
    alert "WARN" "内存使用率 ${MEM_PCT}% 超过 90%"
  else
    ok "内存使用率 ${MEM_PCT}%"
  fi
fi

# ── 汇总 ────────────────────────────────────────────────────────────────────
TOTAL=$((PASS+FAIL))
echo -e "\\n${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  汇总: 通过 ${PASS} | 失败 ${FAIL} | 总计 ${TOTAL}${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"

# ── Slack 告警 ────────────────────────────────────────────────────────────
if [ ${#ALERTS[@]} -gt 0 ] && [ -n "$SLACK_WEBHOOK_URL" ]; then
  SUMMARY="ECS Monitor $(date '+%Y-%m-%d %H:%M')\\n"
  SUMMARY+="通过: $PASS | 失败: $FAIL\\n"
  for a in "${ALERTS[@]}"; do
    SUMMARY+="• $a\\n"
  done
  send_slack_alert "${FAIL}" "🚨 ECS 监控告警" "$SUMMARY"
  echo -e "\\n  ${YELLOW}Slack 告警已发送${NC}"
fi

if [ $FAIL -gt 0 ]; then
  exit 1
fi
exit 0
