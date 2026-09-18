#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/test-mobile-rn.sh —— mobile-rn 用例运行器
#
# 用途    : 与 test-frontend.sh / test-mobile.sh 同一口径，目标换成 mobile-rn（Jest + RNTL）。
# 用法    : ./scripts/test-mobile-rn.sh                          # 全量
#           ./scripts/test-mobile-rn.sh src/__tests__/paths.test.ts
#           PA_TEST_TIMEOUT=300 ./scripts/test-mobile-rn.sh
# 判定口径: **以每个文件的 PASS/FAIL 行为准**，而不是退出码 —— jest 在本沙箱跑完会打印
#           "Jest did not exit one second after the test run has completed"（React Query / Modal 的
#           定时器句柄），退出码因此不是可信信号（与前端两个套件同一既有现象）。
# 提前收尾: 轮询日志，一旦"预期文件数都 PASS"或"出现 FAIL"立即收掉进程组。
# 退出码  : 0 = 所有已发现文件都 PASS 且无 FAIL；1 = 有失败/超时/文件数不符。
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PAD_LOG_DIR:-${TMPDIR:-/tmp}}"
LOG="$LOG_DIR/mobile-rn-tests.log"
CLEAN="$LOG.clean"
LIMIT="${PA_TEST_TIMEOUT:-180}"

tag='[mobile-rn-test]'
info() { printf '\033[36m%s\033[0m %s\n' "$tag" "$*"; }
ok()   { printf '\033[32m%s\033[0m %s\n' "$tag" "$*"; }
err()  { printf '\033[31m%s\033[0m %s\n' "$tag" "$*" >&2; }

cd "$ROOT/mobile-rn" || { err "找不到 mobile-rn/ 目录"; exit 1; }
mkdir -p "$LOG_DIR"

if [ "$#" -gt 0 ]; then
  expected="$#"
  info "运行指定文件（$#）：$*"
else
  expected=$(ls src/__tests__/*.test.ts src/__tests__/*.test.tsx 2>/dev/null | wc -l | tr -d ' ')
  info "运行全量（$expected 个文件）；上限 ${LIMIT}s；日志 $LOG（可另开终端 tail -f 跟进度）"
fi

# 去 ANSI 后统计文件级结果（jest 的 PASS/FAIL 行）
scrape() {
  sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' "$LOG" > "$CLEAN"
  files_ok=$(grep -acE '^PASS src/__tests__/' "$CLEAN")
  files_fail=$(grep -acE '^FAIL src/__tests__/' "$CLEAN")
}

: > "$LOG"
setsid bash -c 'exec npx jest "$@"' _ "$@" > "$LOG" 2>&1 &
run_pid=$!

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

# 收掉进程组（jest 会起 worker），再等它落盘
kill -TERM -"$run_pid" 2>/dev/null
sleep 1
kill -KILL -"$run_pid" 2>/dev/null
wait "$run_pid" 2>/dev/null

scrape
summary=$(grep -aE '^Tests: +[0-9]+ (passed|failed)' "$CLEAN" | tail -1)

if [ "$files_fail" -ne 0 ]; then
  err "有失败文件（详见 $LOG）"
  grep -aE '^FAIL src/__tests__/' "$CLEAN" | head -10 >&2
  exit 1
fi
if [ "$files_ok" -lt "$expected" ]; then
  if [ "$SECONDS" -ge "$deadline" ]; then
    err "超时 ${LIMIT}s：只见到 $files_ok/$expected 个文件 PASS（用 PA_TEST_TIMEOUT 调大或分组跑）"
  else
    err "文件数不符：$files_ok/$expected 个文件 PASS（详见 $LOG）"
  fi
  exit 1
fi

ok "全部通过：$files_ok/$expected 个文件 PASS（用时约 $((SECONDS % 10000))s）"
ok "汇总行：${summary:-（未打印最终汇总，以文件级 PASS 为准）}；日志：$LOG"
