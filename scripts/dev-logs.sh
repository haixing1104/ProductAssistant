#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/dev-logs.sh —— 跟进 dev-up.sh 的四份日志（带服务前缀）
#
# 用途    : 后台模式（dev-up.sh --detach）或事后排查时，在一个终端里跟进四路日志。
#           前缀与 dev-up 前台模式一致：青 [backend] / 品红 [ai-engine] / 绿 [frontend] / 黄 [mobile]。
# 用法    : ./scripts/dev-logs.sh              # 跟随全部（等价 all）
#           ./scripts/dev-logs.sh backend      # 只看后端（AI 引擎 / 前端 / 移动端同理）
#           ./scripts/dev-logs.sh all -n 200   # 先回看最后 200 行再跟随
# 说明    : tail --pid 不在此处使用（进程可能尚未启动）；Ctrl-C 只结束本脚本，不影响服务。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PAD_LOG_DIR:-${TMPDIR:-/tmp}/padev}"

TARGET="${1:-all}"
shift || true
LINES=40
if [ "${1:-}" = "-n" ] && [ -n "${2:-}" ]; then LINES="$2"; shift 2; fi

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BACKEND=$'\033[36m'; C_AI=$'\033[35m'; C_FRONT=$'\033[32m'; C_MOBILE=$'\033[33m'; C_ERR=$'\033[31m'
else
  C_RESET=""; C_BACKEND=""; C_AI=""; C_FRONT=""; C_MOBILE=""; C_ERR=""
fi

color_for() {
  case "$1" in
    backend)   printf '%s' "${C_BACKEND}" ;;
    ai-engine) printf '%s' "${C_AI}" ;;
    frontend)  printf '%s' "${C_FRONT}" ;;
    mobile)    printf '%s' "${C_MOBILE}" ;;
    *)         printf '%s' "" ;;
  esac
}

follow_one() {
  local name="$1" file="${LOG_DIR}/${1}.log"
  if [ ! -f "${file}" ]; then
    printf '%s[%s] 暂无日志（%s 未启动过？）%s\n' "${C_ERR}" "${name}" "${file}" "${C_RESET}" >&2
    return 0
  fi
  tail -n "${LINES}" -F "${file}" 2>/dev/null | sed -u -e "s/^/$(color_for "${name}")[${name}]${C_RESET} /" &
}

case "${TARGET}" in
  all)       follow_one backend; follow_one ai-engine; follow_one frontend; follow_one mobile ;;
  backend|ai-engine|frontend|mobile) follow_one "${TARGET}" ;;
  -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "未知目标: ${TARGET}（支持 backend / ai-engine / frontend / mobile / all）" >&2; exit 2 ;;
esac

sleep 0.3   # 让 tail 先吐出回看的若干行，再打提示（否则提示会插在日志前面）
echo "（Ctrl-C 退出日志跟进；服务继续运行。日志目录 ${LOG_DIR}）"
wait

