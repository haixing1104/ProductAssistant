#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/dev-up.sh —— 一键启动本地开发环境（三进程 + 完整 RAG 链路）
#
# 用途    : 一条命令起全栈，并把四个进程的日志**实时**打到当前终端（带前缀区分）：
#             [backend]   uvicorn pa_backend.main:app   （:8000，统一信封 + SSE + 三个常驻任务）
#             [ai-engine] python -m src.service         （消费 job:* / 投递 result 与 evt 事件）
#             [frontend]  npm run dev                   （:5173，/api 代理到 :8000）
#             [mobile]    npm run dev                   （:5174，移动端 H5，同样代理 /api）
#           同时日志落盘到 ${TMPDIR:-/tmp}/padev/*.log，终端看得实时、事后可回溯。
#
# 停止    : 前台模式直接 Ctrl-C（脚本会收干净三个进程及其子进程）；
#           后台模式（--detach）用 ./scripts/dev-down.sh；跟进日志用 ./scripts/dev-logs.sh。
#           本脚本启动前也会检测「是否已在运行」，避免重复起（两个 worker 会抢同一消费组 PEL）。
# 宣传页  : 作品集宣传页（portfolio/，:5175）**不在本脚本内** —— 它零后端依赖、常年独立部署，
#           混进这条进程组只会让 dev-down/dev-logs 多一份要收的边界；要用时单独起：./scripts/dev-landing.sh
#
# 用法    : ./scripts/dev-up.sh [选项]
#             （无参数）      全起：基础设施 + backend + ai-engine + frontend + mobile（前台）
#             --detach       后台运行（日志仅落盘），随后用 dev-logs.sh 跟进
#             --no-ai        只起 backend + frontend + mobile（跳过 ai-engine 与 Milvus 链路）
#             --no-mobile    不起移动端 H5（省内存；桌面端 http.ts/sse.ts 与它共享同一份代码）
#             --no-infra     不动容器（自己管 Redis/Milvus）
#             --no-reload    不起 uvicorn --reload（排查 reload 干扰时用）
#             --strict-env   env-check.sh 有 WARN 也阻断（默认仅提示）
#             -h, --help     显示帮助
#
# 就绪判据（**探真实依赖，不看进程存活**，避免「进程活着但依赖没好」的假成功）:
#   · backend  → GET /readyz == 200（PG + Redis 都通才算就绪；/healthz 只证明进程在）
#   · frontend → GET / 200 且响应含 id="root"（vite 已能编译出 SPA 骨架）
#   · mobile   → GET :5174/ 200 且响应含 id="root"（移动端 H5 可编译；真机用 http://<本机IP>:5174）
#   · ai-engine→ Redis 出现 pa:{env}:worker:heartbeat:*（复用 P6 运维口径 = 真在消费）
#   · milvus   → 容器 Health.Status == healthy（compose healthcheck 探 9091/healthz）
#
# 基础设施（--no-infra 可跳过）: infra/docker-compose.yml 的 redis + etcd/minio/milvus
#   · redis 缺失自动拉起（幂等）；etcd/minio/milvus 一并拉起以启用完整 RAG 召回；
#   · Milvus 依赖 etcd/minio 先 healthy（compose depends_on 已声明），首启需拉镜像、较慢；
#   · 集合初始化走 database/milvus/init_collections.py（**幂等**：集合已存在则跳过），
#     用 ai-engine 的 venv 执行（pymilvus 装在那里）；
#   · Milvus 未就绪**不阻塞**启动，只告警「RAG 降级」——worker 本身支持降级运行，
#     硬失败会让人连页面都上不去（这与 ai-engine 的容错设计一致）。
#
# 依赖    : docker + compose v2、curl、pg_isready、ss、setsid（util-linux）、python3、node
# 注意    : 只输出键名与状态，**绝不打印键值**（避免密钥进入终端与日志）。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

LOG_DIR="${PAD_LOG_DIR:-${TMPDIR:-/tmp}/padev}"
STATE_DIR="${LOG_DIR}"            # pidfile / pgid 与日志同目录，便于一起清理
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.yml"
INFRA_ENV="${ROOT_DIR}/infra/.env"
BACKEND_DIR="${ROOT_DIR}/backend-api"
AI_DIR="${ROOT_DIR}/ai-engine"
FRONTEND_DIR="${ROOT_DIR}/frontend"
MOBILE_DIR="${ROOT_DIR}/mobile-h5"

# 后端监听地址（PA_BACKEND_HOST）：
#   默认 127.0.0.1 = 只监听回环（**安全默认，不要随手改**）—— 桌面端与 H5 的 dev 都走同源代理
#   （vite `/api` → 127.0.0.1:8000，请求由开发机发出），回环就够，且不会把开发机上的真实数据
#   暴露给同一局域网的其它设备。
#   但移动端原生 App（mobile-rn）**没有任何代理**：请求从手机直接发出，所以真机联调必须让后端
#   暴露到局域网，否则表现是「手机能连上 Metro、一点登录就报网络异常」（2026-09 实测踩过）。
#   联调写法：PA_BACKEND_HOST=0.0.0.0 ./scripts/dev-up.sh（联调完请恢复默认）。
BACKEND_HOST="${PA_BACKEND_HOST:-127.0.0.1}"

# ---------------- 选项 ----------------
DETACH=0
WITH_AI=1
WITH_INFRA=1
WITH_MOBILE=1
RELOAD=1
STRICT_ENV=0

usage() {
  cat <<'EOF'
用法: ./scripts/dev-up.sh [选项]

  （无参数）    全起：基础设施(redis+etcd+minio+milvus) + backend + ai-engine + frontend + mobile
  --detach      后台运行（日志只落盘）；随后用 ./scripts/dev-logs.sh 跟进
  --no-ai       只起 backend + frontend + mobile（跳过 ai-engine 与 Milvus 链路，省内存）
  --no-mobile   不起移动端 H5（:5174）
  --no-infra    不动容器（Redis/Milvus 你自己管）
  --no-reload   不起 uvicorn --reload
  --strict-env  env-check.sh 出现 WARN 也阻断启动
  -h, --help    显示本帮助

环境变量:
  PA_BACKEND_HOST=<ip>  后端监听地址（默认 127.0.0.1，只监听回环）。移动端原生 App（mobile-rn）
                        真机联调必须设 0.0.0.0 —— RN 没有代理，请求从设备直接发出；联调完请恢复默认。

日志: ${TMPDIR:-/tmp}/padev/{backend,ai-engine,frontend,mobile}.log（可用 PAD_LOG_DIR 覆盖）
      每次启动会把上一轮日志滚成 .log.1（保留 PAD_LOG_KEEP 份，默认 3）—— 不再清空历史现场
移动端: H5  http://localhost:5174（真机：http://<本机局域网IP>:5174 —— 走 vite 代理，无需 CORS）
        RN  mobile-rn/.env 写 EXPO_PUBLIC_API_BASE_URL=http://<本机局域网IP>:8000
            （原生端没有代理，必须绝对地址；且后端须以 PA_BACKEND_HOST=0.0.0.0 启动，
              否则表现是「手机能连 Metro、一登录就网络异常」；改完 .env 必须 npx expo start -c）
宣传页: 不由本脚本启动（零后端依赖）：./scripts/dev-landing.sh → http://localhost:5175
停止: 前台 Ctrl-C；后台 ./scripts/dev-down.sh

EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --detach)     DETACH=1 ;;
    --no-ai)      WITH_AI=0 ;;
    --no-mobile)  WITH_MOBILE=0 ;;
    --no-infra)   WITH_INFRA=0 ;;
    --no-reload)  RELOAD=0 ;;
    --strict-env) STRICT_ENV=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

# ---------------- 输出与颜色（重定向到文件时不着色）----------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=""; C_DIM=""; C_OK=""; C_ERR=""; C_STEP=""
fi

STEP_NO=0
step() { STEP_NO=$((STEP_NO + 1)); echo "${C_STEP}▶ [${STEP_NO}] $*${C_RESET}"; }
ok()   { echo "  ${C_OK}✔${C_RESET} $*"; }
warn() { echo "  ${C_DIM}!${C_RESET} $*"; }
fail() { echo "  ${C_ERR}✘${C_RESET} $*" >&2; }

CURRENT_STEP="初始化"
on_err() {
  local rc=$?
  echo "" >&2
  fail "dev-up 失败 —— 失败于步骤：${CURRENT_STEP}（rc=${rc}）"
  echo "     · 日志：tail -n 50 ${LOG_DIR}/backend.log（或 ai-engine.log / frontend.log / mobile.log）" >&2
  echo "     · 清理：./scripts/dev-down.sh（停四进程；加 --with-infra 连容器一起停）" >&2
  exit "${rc}"
}
trap on_err ERR

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$1" 2>/dev/null || echo 000; }
port_busy() { ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN; }
port_owner() { ss -ltnp "( sport = :$1 )" 2>/dev/null | sed -n '2p' | sed -E 's/.*users:\(\("([^"]+)".*/\1/'; }
container_health() { docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || echo none; }

# lan_ip：本机在局域网里的地址（只用于打印「真机联调该填哪个地址」，不参与任何判定）。
#   · 首选 `ip route get`：它给的是**默认路由实际使用的源地址** —— WSL2 镜像模式（.wslconfig 的
#     networkingMode=mirrored）下直接得到宿主机 Wi-Fi IP，正是手机可达的那个地址；
#     NAT 模式下得到的是 WSL 虚拟网段地址（手机本来就到不了，见 mobile-rn/README「WSL2 联调」）；
#   · 回落 `hostname -I` 的第一个（多网卡/容器环境里不保证正确，仅兜底）。
lan_ip() {
  local ip=""
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || true)"
  if [ -z "${ip}" ]; then ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; fi
  printf '%s' "${ip}"
}

# wait_until <说明> <超时秒> <探测命令...>：轮询探测，每 10s 打印一次已等待秒数（长步骤不像是卡死）
wait_until() {
  local label="$1" timeout_s="$2"; shift 2
  local waited=0
  while [ "${waited}" -lt "${timeout_s}" ]; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 2; waited=$((waited + 2))
    if [ $((waited % 10)) -eq 0 ]; then
      printf '  %s… 已等待 %ss/%ss\n' "${label}" "${waited}" "${timeout_s}"
    fi
  done
  return 1
}

# rotate_log <日志文件>：把上一轮日志滚成 .1/.2/…（保留 PAD_LOG_KEEP 份）后新建空文件
#
# 为什么不再 `: > logfile` 直接清空：历史日志是排查的唯一线索 —— 2026-09 查「生成卡住」时，
# 现场日志已被上一次 dev-up 清掉，只能靠 Redis 消息 ID 与 DB updated_at 反推时间，代价很高。
rotate_log() {
  local file="$1" keep="${PAD_LOG_KEEP:-3}" i
  [ -f "${file}" ] || return 0
  [ -s "${file}" ] || return 0            # 空文件不必滚
  i=$((keep - 1))
  while [ "${i}" -ge 1 ]; do
    [ -f "${file}.${i}" ] && mv -f "${file}.${i}" "${file}.$((i + 1))"
    i=$((i - 1))
  done
  mv -f "${file}" "${file}.1"
}

# start_service <名> <工作目录> <颜色> <命令...>：独立会话/进程组 + 前缀实时日志 + 落盘
#
# 为什么用「生成 runner 脚本 + setsid --fork + 服务自己写 pid」：
#   · 管道里 $! 是 tee 而不是服务本身，靠它拿不到真实 pid；
#   · vite 会 fork node 子进程、uvicorn --reload 会 fork reloader 子进程 ——
#     必须按**进程组**停止，否则会留下孤儿占住 8000/5173；
#   · setsid --fork 后新会话 leader 的 **pgid 必等于自身 pid**（setsid(2) 保证），
#     于是 `kill -TERM -$pid` 就能连子进程一起收干净，不依赖 ps 的时序与 leader 判定。
declare -A SVC_PID=()
COLOR_BACKEND=$'\033[36m'; COLOR_AI=$'\033[35m'; COLOR_FRONT=$'\033[32m'; COLOR_MOBILE=$'\033[33m'

start_service() {
  local name="$1" workdir="$2" color="$3"; shift 3
  local logfile="${LOG_DIR}/${name}.log" pidfile="${STATE_DIR}/${name}.pid"
  local runner="${LOG_DIR}/${name}.runner.sh"
  rotate_log "${logfile}"
  rm -f "${pidfile}"
  {
    echo '#!/usr/bin/env bash'
    printf 'cd %q\n' "${workdir}"
    printf 'echo $$ > %q\n' "${pidfile}"
    printf 'exec'
    printf ' %q' "$@"
    echo
  } > "${runner}"
  chmod +x "${runner}"
  setsid --fork "${runner}" \
    > >(tee -a "${logfile}" | sed -u -e "s/^/${color}[${name}]${C_RESET} /") 2>&1
  # 说明：先 tee 落盘（**原始**行，便于 grep/回溯），再由 sed 加前缀只作用于终端显示 ——
  # 反过来（先 sed 后 tee）会把 ANSI 前缀写进日志文件，dev-logs 再跟进时就成了双层前缀。
  local pid="" waited=0
  while [ "${waited}" -lt 10 ]; do
    [ -s "${pidfile}" ] && { pid="$(cat "${pidfile}")"; break; }
    sleep 0.3; waited=$((waited + 1))
  done
  if [ -z "${pid}" ]; then
    fail "${name} 未能写出 pid（见 ${logfile}）"
    return 1
  fi
  SVC_PID["${name}"]="${pid}"
  echo "${pid}" > "${STATE_DIR}/${name}.pgid"   # 独立会话：pgid == pid，故两个文件同值
  ok "${name} 已启动（pid/pgid=${pid}，日志 ${logfile}）"
}

stop_services() {
  local name file pgid
  for name in "$@"; do
    file="${STATE_DIR}/${name}.pgid"
    [ -f "${file}" ] || continue
    pgid="$(cat "${file}")"
    if [ -n "${pgid}" ] && kill -0 "-${pgid}" 2>/dev/null; then
      kill -TERM "-${pgid}" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "-${pgid}" 2>/dev/null || break
        sleep 0.2
      done
      kill -KILL "-${pgid}" 2>/dev/null || true
      echo "  已停止 ${name}（pgid=${pgid}）"
    fi
    rm -f "${file}" "${STATE_DIR}/${name}.pid"
  done
}

# 兜底回收：pidfile 之外的残留（旧 run、手工起过的进程）；[x] 写法避免匹配到本脚本自身
pkill_leftovers() {
  pkill -f "${ROOT_DIR}/backend-api/.venv/bin/[u]vicorn" 2>/dev/null || true
  pkill -f "pa_backend.main:app" 2>/dev/null || true
  pkill -f "${ROOT_DIR}/ai-engine/.venv/bin/[p]ython -m src.service" 2>/dev/null || true
  pkill -f "${ROOT_DIR}/frontend/node_modules/[.]bin/vite" 2>/dev/null || true
  # 移动端 H5（同款 vite，但 node_modules 在 mobile-h5/ 下 —— 路径必须分开匹配，
  # 否则 mobile 的 vite 会被漏掉：dev-down 后 5174 仍占着，下次启动直接端口冲突）
  pkill -f "${ROOT_DIR}/mobile-h5/node_modules/[.]bin/vite" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  echo ""
  echo "${C_STEP}▶ 正在停止四进程…${C_RESET}"
  stop_services mobile frontend ai-engine backend
  pkill_leftovers
  echo "  已全部停止（日志保留在 ${LOG_DIR}）"
  exit "${rc}"
}

# ---------------------------------------------------------------------------
# S0 前置自检
# ---------------------------------------------------------------------------
CURRENT_STEP="S0 前置自检"
step "S0 前置自检"

if [ ! -f "${INFRA_ENV}" ]; then
  fail "缺少 ${INFRA_ENV}（环境变量唯一入库契约是 infra/.env.template）"
  echo "     处置：./infra/scripts/env-init.sh（按模板补齐缺键后再跑本脚本）" >&2
  exit 1
fi
# 与 pgsql-setup.sh 同写法：把 infra/.env 导出到进程环境
# （backend 的 pydantic settings 找的是 cwd 的 .env，而本仓约定是集中放在 infra/.env）
set -a; . "${INFRA_ENV}"; set +a
ok "已加载 infra/.env（PA_ENV=${PA_ENV:-dev}，BACKEND_PORT=${BACKEND_PORT:-8000}）"

if [ -x "${ROOT_DIR}/infra/scripts/env-check.sh" ]; then
  if "${ROOT_DIR}/infra/scripts/env-check.sh" >"${LOG_DIR}/env-check.log" 2>&1; then
    ok "env-check 通过（详见 ${LOG_DIR}/env-check.log）"
  elif [ "${STRICT_ENV}" = "1" ]; then
    fail "env-check 未通过（--strict-env）；详见 ${LOG_DIR}/env-check.log"
    exit 1
  else
    warn "env-check 有告警但不阻断（加 --strict-env 可阻断）；详见 ${LOG_DIR}/env-check.log"
  fi
fi

for entry in \
  "backend-api/.venv|cd backend-api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt" \
  "ai-engine/.venv|cd ai-engine && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" \
  "frontend/node_modules|cd frontend && npm install" \
  "mobile-h5/node_modules|cd mobile-h5 && npm install"; do
  path="${entry%%|*}"; hint="${entry#*|}"
  if [ ! -e "${ROOT_DIR}/${path}" ]; then
    fail "缺少 ${path}（依赖未安装）"
    echo "     处置：${hint}" >&2
    exit 1
  fi
done
ok "依赖目录齐备（backend-api/.venv、ai-engine/.venv、frontend/node_modules、mobile-h5/node_modules）"

if ! pg_isready -h "${POSTGRES_HOST:-127.0.0.1}" -p "${POSTGRES_PORT:-5432}" >/dev/null 2>&1; then
  fail "PostgreSQL 不可达（${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-5432}）"
  echo "     处置：启动本机 PostgreSQL；首次初始化：./infra/scripts/pgsql-setup.sh init" >&2
  exit 1
fi
ok "PostgreSQL 可达（数据库 ${POSTGRES_DB:-productassistant}）"

BLOCKERS=0
PORTS_TO_CHECK=("${BACKEND_PORT:-8000}" 5173)
[ "${WITH_MOBILE}" = "1" ] && PORTS_TO_CHECK+=(5174)
for port in "${PORTS_TO_CHECK[@]}"; do
  if port_busy "${port}"; then
    fail "端口 ${port} 已被占用（$(port_owner "${port}" || true)）"
    BLOCKERS=1
  fi
done
if [ "${BLOCKERS}" = "1" ]; then
  echo "     本脚本不静默换端口：8000 写在 vite 代理里、5173/5174 写在文档与 strictPort 约定里。" >&2
  echo "     处置：./scripts/dev-down.sh（收掉上次遗留进程），或手工停掉占用者后重试。" >&2
  exit 1
fi
ok "端口 $(IFS=/; echo "${PORTS_TO_CHECK[*]}") 空闲"

SERVICE_NAMES=(backend ai-engine frontend)
[ "${WITH_MOBILE}" = "1" ] && SERVICE_NAMES+=(mobile)
for name in "${SERVICE_NAMES[@]}"; do
  if [ -f "${STATE_DIR}/${name}.pgid" ] && kill -0 "-$(cat "${STATE_DIR}/${name}.pgid")" 2>/dev/null; then
    fail "${name} 似乎已在运行（pgid=$(cat "${STATE_DIR}/${name}.pgid")）"
    echo "     处置：./scripts/dev-down.sh 后再启动（两个 worker 会抢同一消费组 PEL）" >&2
    exit 1
  fi
done
ok "无上次遗留的本项目进程"

# ---------------------------------------------------------------------------
# S1 基础设施（redis + etcd/minio/milvus → 集合初始化）
# ---------------------------------------------------------------------------
CURRENT_STEP="S1 基础设施"
step "S1 基础设施（redis + etcd/minio/milvus）"

redis_ping() { docker exec pa-redis redis-cli ping 2>/dev/null | grep -q PONG; }
compose_up() { (cd "${ROOT_DIR}" && docker compose -f "${COMPOSE_FILE}" up -d "$@") >"${LOG_DIR}/compose.log" 2>&1; }

# ensure_container <容器名> <compose 服务名>：把容器弄到「运行中」，不重复创建
#
# 为什么要区分「已存在」与「不存在」：
#   本仓的 Redis 常以 `docker run` 手工起（或由环境预置），这类容器**没有 compose 标签**；
#   此时再 `compose up redis` 会因容器名冲突而失败（实测踩过）。
#   因此：已存在 → 直接 `docker start`（幂等、不碰 compose）；不存在 → 才交给 compose 创建。
ensure_container() {
  local cname="$1" svc="$2"
  if docker container inspect "${cname}" >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' "${cname}")" = "true" ]; then
      echo "  · ${cname} 已在运行（复用）"
    else
      echo "  · ${cname} 已存在但未运行 → docker start（不走 compose，避免同名冲突）"
      docker start "${cname}" >/dev/null 2>&1 || true
    fi
  else
    echo "  · 创建并启动 ${cname}（compose 服务 ${svc}；首次拉镜像可能需要一会儿，日志 ${LOG_DIR}/compose.log）"
    compose_up "${svc}" || { fail "${cname} 启动失败，详见 ${LOG_DIR}/compose.log"; exit 1; }
  fi
}

if [ "${WITH_INFRA}" = "0" ]; then
  warn "--no-infra：不动容器（请自行保证 Redis/Milvus 可用）"
else
  if redis_ping; then
    ok "Redis 已在运行（容器 pa-redis）"
  else
    ensure_container pa-redis redis
    if wait_until "等待 Redis 就绪" 60 redis_ping; then ok "Redis 就绪"; else
      fail "Redis 60s 内未就绪（docker logs pa-redis --tail 20）"; exit 1
    fi
  fi

  if [ "${WITH_AI}" = "0" ]; then
    warn "--no-ai：跳过 etcd/minio/milvus（ai-engine 不启动，RAG 链路不需要）"
  else
    echo "  · 拉起 Milvus 组（etcd + minio + milvus）：Milvus 有 30s 启动缓冲，"
    echo "    长等待会每 10s 打印进度（不像是卡死）"
    ensure_container pa-etcd etcd
    ensure_container pa-minio minio
    ensure_container pa-milvus milvus

    etcd_healthy() { [ "$(container_health pa-etcd)" = "healthy" ]; }
    minio_healthy() { [ "$(container_health pa-minio)" = "healthy" ]; }
    milvus_healthy() { [ "$(container_health pa-milvus)" = "healthy" ]; }
    wait_until "等待 etcd healthy" 120 etcd_healthy || warn "etcd 120s 内未 healthy（Milvus 可能起不来）"
    wait_until "等待 minio healthy" 120 minio_healthy || warn "minio 120s 内未 healthy（Milvus 可能起不来）"

    if wait_until "等待 Milvus healthy" 240 milvus_healthy; then
      ok "Milvus healthy（healthy=容器 healthcheck 探通 9091/healthz）"
      # 集合初始化（幂等：集合已存在则跳过）—— 必须在 worker 之前，否则 RAG 会静默降级
      if "${AI_DIR}/.venv/bin/python" "${ROOT_DIR}/database/milvus/init_collections.py" \
           --uri "${MILVUS_URI:-http://localhost:19530}" --dim "${MILVUS_DIM:-1024}" \
           >"${LOG_DIR}/milvus-init.log" 2>&1; then
        ok "Milvus 集合就绪（pa_listing_vec；详见 ${LOG_DIR}/milvus-init.log）"
      else
        warn "集合初始化失败（详见 ${LOG_DIR}/milvus-init.log）→ RAG 将降级，但启动继续"
      fi
    else
      fail "Milvus 240s 内未 healthy → RAG 将降级（启动继续，不影响页面与生成主链路）"
      echo "     常见原因与处置：" >&2
      echo "       · MinIO 凭据不匹配：docker logs pa-milvus --tail 30 | grep -i -E 'minio|access'" >&2
      echo "       · 内存不足：Milvus standalone 建议 ≥4GB 空闲内存" >&2
      docker logs pa-milvus --tail 15 >"${LOG_DIR}/milvus-last.log" 2>&1 || true
      echo "       · 最近日志已存到 ${LOG_DIR}/milvus-last.log" >&2
    fi
  fi
fi

# ---------------------------------------------------------------------------
# S2 启动三进程（独立进程组 + 前缀实时日志）
# ---------------------------------------------------------------------------
CURRENT_STEP="S2 启动三进程"
step "S2 启动三进程"
export PYTHONUNBUFFERED=1   # 关键：保证 Python 日志**逐行实时**（--reload 的子进程同样继承）

BACKEND_ARGS=(pa_backend.main:app --app-dir src --host "${BACKEND_HOST}" --port "${BACKEND_PORT:-8000}")
[ "${RELOAD}" = "1" ] && BACKEND_ARGS+=(--reload)
if [ "${BACKEND_HOST}" != "127.0.0.1" ]; then
  warn "后端监听 ${BACKEND_HOST}（非回环）：同一局域网内任何设备都能访问 :8000（含真实数据）—— 联调完请恢复默认"
fi

start_service backend  "${BACKEND_DIR}"  "${COLOR_BACKEND}" \
  "${BACKEND_DIR}/.venv/bin/uvicorn" "${BACKEND_ARGS[@]}"

if [ "${WITH_AI}" = "1" ]; then
  start_service ai-engine "${AI_DIR}" "${COLOR_AI}" \
    "${AI_DIR}/.venv/bin/python" -m src.service
else
  warn "--no-ai：未启动 ai-engine（生成链路不会被执行，商品会停在 generating、运维面板显示 stalled）"
fi

start_service frontend "${FRONTEND_DIR}" "${COLOR_FRONT}" npm run dev

if [ "${WITH_MOBILE}" = "1" ]; then
  start_service mobile "${MOBILE_DIR}" "${COLOR_MOBILE}" npm run dev
else
  warn "--no-mobile：未启动移动端 H5（:5174）"
fi

if [ "${DETACH}" = "1" ]; then
  ok "--detach：脚本在后台运行，日志只落盘（跟进：./scripts/dev-logs.sh）"
fi

# ---------------------------------------------------------------------------
# S3 就绪探测（探真实依赖，而非进程存活）
# ---------------------------------------------------------------------------
CURRENT_STEP="S3 就绪探测"
step "S3 就绪探测（/readyz 探 PG+Redis；心跳探消费侧；SPA 骨架探前端）"

readyz_ok() { [ "$(http_code "http://127.0.0.1:${BACKEND_PORT:-8000}/readyz")" = "200" ]; }
frontend_ok() { [ "$(http_code http://127.0.0.1:5173/)" = "200" ]; }
# 移动端判据与桌面端同口径，只是换端口：200 + 响应含 id="root"（vite 已能编译出 SPA 骨架）
mobile_ok() { [ "$(http_code http://127.0.0.1:5174/)" = "200" ]; }
heartbeat_ok() { docker exec pa-redis redis-cli --scan --pattern "pa:${PA_ENV:-dev}:worker:heartbeat:*" 2>/dev/null | grep -q heartbeat; }

if wait_until "等待 backend /readyz" 90 readyz_ok; then
  ok "backend 就绪（/readyz 200 = PG + Redis 都通）"
else
  fail "backend 90s 内未就绪：tail -n 40 ${LOG_DIR}/backend.log"
  exit 1
fi

if wait_until "等待 frontend" 90 frontend_ok; then
  ok "frontend 就绪（:5173 返回 SPA 骨架）"
else
  fail "frontend 90s 内未就绪：tail -n 40 ${LOG_DIR}/frontend.log"
  exit 1
fi

if [ "${WITH_MOBILE}" = "1" ]; then
  if wait_until "等待 mobile" 90 mobile_ok; then
    ok "mobile 就绪（:5174 返回 SPA 骨架；真机访问 http://<本机IP>:5174）"
  else
    fail "mobile 90s 内未就绪：tail -n 40 ${LOG_DIR}/mobile.log"
    exit 1
  fi
fi

if [ "${WITH_AI}" = "1" ]; then
  if wait_until "等待 ai-engine 心跳" 60 heartbeat_ok; then
    ok "ai-engine 心跳已出现（pa:${PA_ENV:-dev}:worker:heartbeat:* = 真在消费）"
  else
    warn "60s 内未见心跳：tail -n 40 ${LOG_DIR}/ai-engine.log（运维面板会显示 stalled）"
  fi
fi

# ---------------------------------------------------------------------------
# S4 摘要
# ---------------------------------------------------------------------------
CURRENT_STEP="S4 摘要"
echo ""

# 真机联调要用的两个地址（拿不到本机局域网 IP 时留空，下面按空值退化显示）
LAN_IP="$(lan_ip)"
LAN_API="http://${LAN_IP}:${BACKEND_PORT:-8000}"
H5_HINT="http://<本机局域网IP>:5174"
[ -n "${LAN_IP}" ] && H5_HINT="http://${LAN_IP}:5174"
echo "=============================================================="
echo " ProductAssistant 本地环境已就绪"
echo "--------------------------------------------------------------"
echo "  前端工作台 : http://localhost:5173      （首次需点「注册新企业」开租户）"
if [ "${WITH_MOBILE}" = "1" ]; then
echo "  移动端 H5  : http://localhost:5174      （真机：${H5_HINT}，走 vite 代理无需 CORS）"
fi
echo "  作品集宣传页 : http://localhost:5175      （不在本脚本内：另开终端 ./scripts/dev-landing.sh）"
echo "  后端 API   : http://localhost:${BACKEND_PORT:-8000}/healthz  ·  /readyz  ·  /docs"
if [ -n "${LAN_IP}" ]; then
  echo "  RN 真机联调 : mobile-rn/.env → EXPO_PUBLIC_API_BASE_URL=${LAN_API}"
  if [ "${BACKEND_HOST}" = "127.0.0.1" ]; then
    echo "                ⚠ 后端当前只监听 127.0.0.1（手机到不了）→ 请以 PA_BACKEND_HOST=0.0.0.0 重启"
  else
    echo "                自检：手机浏览器打开 ${LAN_API}/healthz；改完 .env 必须 npx expo start -c"
  fi
fi
if [ "${WITH_AI}" = "1" ]; then
echo "  AI 引擎    : worker 心跳见 /ops 页面（运维面板，仅 admin）"
echo "  Milvus     : http://localhost:9091/healthz · MinIO 控制台 http://localhost:9001"
fi
echo "--------------------------------------------------------------"
echo "  日志       : ${LOG_DIR}/{backend,ai-engine,frontend,mobile}.log"
echo "  停止       : 前台 Ctrl-C"
echo "  仅后台日志 : ./scripts/dev-logs.sh [backend|ai-engine|frontend|mobile|all]"
echo "=============================================================="
echo ""

if [ "${DETACH}" = "1" ]; then
  exit 0
fi

# 前台模式：守护循环。
# 注意：服务跑在**独立会话**里（setsid --fork），已不是本脚本的子进程，因此不能用 wait；
# 改为轮询三个 pid：全没了就退出（顺便覆盖「某个服务自己崩了」的情况）。
trap cleanup INT TERM
while :; do
  alive=0
  for name in "${SERVICE_NAMES[@]}"; do
    pid="$(cat "${STATE_DIR}/${name}.pid" 2>/dev/null || true)"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then alive=1; fi
  done
  if [ "${alive}" = "0" ]; then
    warn "三个服务都已退出（日志见 ${LOG_DIR}）"
    break
  fi
  sleep 2
done
stop_services frontend ai-engine backend


