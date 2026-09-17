#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/test-frontend.sh
# 用途    : 前端用例运行器 —— 解决「vitest 全量跑：用例全绿但不打印汇总、进程不退出」
# 用法    : ./scripts/test-frontend.sh                # 全量（本沙箱下必然"收不了尾"）
#           ./scripts/test-frontend.sh src/__tests__/productDetailPage.test.tsx
#           PA_TEST_TIMEOUT=300 ./scripts/test-frontend.sh
# 判定口径: **以每个文件的 ✓/× 行为准**，而不是以退出码或最终汇总行为准 ——
#           本沙箱（Node 24 + vitest 5 + jsdom/antd）下全量跑会在所有用例通过后卡在 teardown
#           （已用对照实验确认与用例内容无关：只跑原有 5 个老文件同样不退出），
#           因此「退出码」在这里并不是可信信号；日志里每个文件的 ✓ 才是。
# 提前收尾: 轮询日志，一旦「预期文件数都出了 ✓」或「出现 ×」就立刻收掉进程组 ——
#           全量跑因此从「等满超时」变成 ~15s（用例本身约 10s 跑完）。
# 退出码  : 0 = 所有已发现文件都 ✓ 且无 ×；1 = 有失败/超时/文件数不符。
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PAD_LOG_DIR:-${TMPDIR:-/tmp}}"
LOG="$LOG_DIR/frontend-tests.log"
CLEAN="$LOG.clean"
LIMIT="${PA_TEST_TIMEOUT:-180}"

tag='[frontend-test]'
info() { printf '\033[36m%s\033[0m %s\n' "$tag" "$*"; }
ok()   { printf '\033[32m%s\033[0m %s\n' "$tag" "$*"; }
err()  { printf '\033[31m%s\033[0m %s\n' "$tag" "$*" >&2; }

cd "$ROOT/frontend" || { err "找不到 frontend/ 目录"; exit 1; }
mkdir -p "$LOG_DIR"

if [ "$#" -gt 0 ]; then
  expected="$#"
  info "运行指定文件（$#）：$*"
else
  expected=$(ls src/__tests__/*.test.ts src/__tests__/*.test.tsx 2>/dev/null | wc -l | tr -d ' ')
  info "运行全量（$expected 个文件）；上限 ${LIMIT}s；日志 $LOG（可另开终端 tail -f 跟进度）"
fi

# 去 ANSI（vitest 的 ✓ 与文件名之间有颜色转义，直接 grep 会漏）+ 统计文件级结果
scrape() {
  sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' "$LOG" > "$CLEAN"
  files_ok=$(grep -acoE '✓ src/__tests__/[A-Za-z0-9_]+\.test\.tsx? \([0-9]+ tests?\)' "$CLEAN")
  files_fail=$(grep -aoE '× src/__tests__/[A-Za-z0-9_]+\.test\.tsx? \([0-9]+ tests?\)' "$CLEAN" | wc -l | tr -d ' ')
}

: > "$LOG"
setsid bash -c 'exec npx vitest run "$@"' _ "$@" > "$LOG" 2>&1 &
run_pid=$!

# 轮询收尾条件（本沙箱里 vitest 跑完不会自己退出）：全 ✓ / 出 × / 超时
deadline=$((SECONDS + LIMIT))
files_ok=0
files_fail=0
while kill -0 "$run_pid" 2>/dev/null; do
  scrape
  [ "$files_fail" -gt 0 ] && break
  [ "$files_ok" -ge "$expected" ] && break
  [ "$SECONDS" -ge "$deadline" ] && break
  sleep 1
done

# 收掉进程组（vite/vitest 会起子进程），再等它落盘
kill -TERM -"$run_pid" 2>/dev/null
sleep 1
kill -KILL -"$run_pid" 2>/dev/null
wait "$run_pid" 2>/dev/null

scrape
summary=$(grep -aE '^ *Test Files +[0-9]+ (passed|failed)' "$CLEAN" | tail -1)

if [ "$files_fail" -ne 0 ]; then
  err "有失败文件（详见 $LOG）"
  grep -aE '× src/__tests__/' "$CLEAN" | head -10 >&2
  exit 1
fi
if [ "$files_ok" -lt "$expected" ]; then
  if [ "$SECONDS" -ge "$deadline" ]; then
    err "超时 ${LIMIT}s：只见到 $files_ok/$expected 个文件 ✓（用 PA_TEST_TIMEOUT 调大或分组跑）"
  else
    err "文件数不符：$files_ok/$expected 个文件 ✓（详见 $LOG）"
  fi
  exit 1
fi

ok "全部通过：$files_ok/$expected 个文件 ✓（用时约 $((SECONDS % 10000))s）"
ok "汇总行：${summary:-（本沙箱未打印最终汇总，以文件级 ✓ 为准）}；日志：$LOG"
