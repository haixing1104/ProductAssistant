#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/dev-landing.sh —— 作品集宣传页（portfolio/）开发入口
#
# 用途    : 前台起宣传页（ :5175 ）以及本模块的校验命令。它**不在 dev-up.sh 的四进程内**：
#           宣传页零后端依赖、常年独立部署，混进「业务全栈 + RAG 链路」的进程组只会让
#           dev-down/dev-logs 多一份要收的边界（收漏了还会把 5175 留给下次启动去撞）。
# 用法    : ./scripts/dev-landing.sh              # npm run dev → http://localhost:5175（Ctrl-C 停）
#           ./scripts/dev-landing.sh --typecheck  # npx tsc --noEmit（必须 0 错误）
#           ./scripts/dev-landing.sh --test       # npm test（Vitest + RTL）
#           ./scripts/dev-landing.sh --test-cov   # 覆盖率（门禁 stmts 80 / branch 75 / func 75 / lines 80，见 portfolio/README）
#           ./scripts/dev-landing.sh --build      # npm run build（tsc --noEmit && vite build）
#           ./scripts/dev-landing.sh --preview    # npm run preview（看构建产物）
#           -h, --help    显示本帮助
# 退出码  : 透传对应 npm/npx 命令的退出码；前置自检失败 = 1；未知参数 = 2。
# 说明    : 端口占用**不静默换端口**（与 dev-up.sh 同口径：5175 写在 vite strictPort 与文档里），
#           占用时直接指名占用者并给处置命令；依赖未装同样给出确切的安装命令。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORTFOLIO_DIR="${ROOT_DIR}/portfolio"
PORT=5175

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=""; C_OK=""; C_ERR=""; C_STEP=""
fi
tag='[landing]'
info() { printf '%s%s%s %s\n' "${C_STEP}" "${tag}" "${C_RESET}" "$*"; }
ok()   { printf '%s%s%s %s\n' "${C_OK}" "${tag}" "${C_RESET}" "$*"; }
err()  { printf '%s%s%s %s\n' "${C_ERR}" "${tag}" "${C_RESET}" "$*" >&2; }

# 帮助直接摘自文件头的注释块（不写死行号：将来加/删一行帮助不会静默错位）
usage() { awk 'NR<=2{next} /^# ====/{exit} {sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"; }

port_busy() { ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN; }
port_owner() { ss -ltnp "( sport = :$1 )" 2>/dev/null | sed -n '2p' | sed -E 's/.*users:\(\("([^"]+)".*/\1/'; }

TASK="dev"
case "${1:-}" in
  ""|--dev)     TASK="dev" ;;
  --typecheck)  TASK="typecheck" ;;
  --test)       TASK="test" ;;
  --test-cov)   TASK="test:cov" ;;
  --build)      TASK="build" ;;
  --preview)    TASK="preview" ;;
  -h|--help)    usage; exit 0 ;;
  *)            err "未知参数: $1"; usage >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  err "只接受一个参数（多了：${*:2}）"
  usage >&2
  exit 2
fi

if [ ! -d "${PORTFOLIO_DIR}" ]; then
  err "找不到 ${PORTFOLIO_DIR}"
  exit 1
fi

if [ ! -d "${PORTFOLIO_DIR}/node_modules" ]; then
  err "缺少 portfolio/node_modules（依赖未安装）"
  echo "     处置：cd portfolio && npm install" >&2
  exit 1
fi

# 只有 dev 争 5175（vite strictPort 写死）；preview 用 vite 自己的默认端口，不做占用检查
if [ "${TASK}" = "dev" ] && port_busy "${PORT}"; then
  err "端口 ${PORT} 已被占用（$(port_owner "${PORT}" || true)）"
  echo "     本模块不静默换端口：5175 写在 vite strictPort 与 portfolio/README 里。" >&2
  echo "     处置：停掉占用者（若是上次遗留的 vite：pkill -f \"${ROOT_DIR}/portfolio/node_modules/[.]bin/vite\"）后重试。" >&2
  exit 1
fi

cd "${PORTFOLIO_DIR}"

if [ "${TASK}" = "typecheck" ]; then
  info "npx tsc --noEmit（类型检查必须 0 错误）"
  exec npx tsc --noEmit
fi

if [ "${TASK}" = "dev" ]; then
  ok "宣传页 dev server：http://localhost:${PORT}（纯静态、无需后端；Ctrl-C 停止）"
  info "卡片上的「开始使用」在 dev 指向本机 :5173（PC Web）/ :5174（Mobile H5）的 /login；"
  info "要真的登进去看数据，另开一个终端先起 ./scripts/dev-up.sh（backend + 两个前端）"
fi
exec npm run "${TASK}"
