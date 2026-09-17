#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | scripts/dev-down.sh —— 停止 dev-up.sh 启动的全部进程
#
# 用途    : 按 pidfile 里的**进程组**停止后端 / AI 引擎 / 前端 / 移动端（连 vite 的 node 子进程、
#           uvicorn --reload 的 reloader 一起收），再用项目级 pkill 兜底回收 pidfile 之外的残留
#           （旧 run 遗留、手工起过的进程），避免 8000/5173/5174 被孤儿进程占住。
# 用法    : ./scripts/dev-down.sh                  # 停四进程（Redis/Milvus 容器保留）
#           ./scripts/dev-down.sh --with-infra     # 连容器一起停（数据卷保留，下次冷启动较慢）
# 设计    : 默认不碰容器 —— Milvus 冷启动要拉镜像 + 30s 起步缓冲，日常收工没必要付这个代价；
#           数据卷始终保留（如需清空：docker compose -f infra/docker-compose.yml down -v）
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
LOG_DIR="${PAD_LOG_DIR:-${TMPDIR:-/tmp}/padev}"

WITH_INFRA=0
case "${1:-}" in
  "")            ;;
  --with-infra)  WITH_INFRA=1 ;;
  -h|--help)     sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "未知参数: $1（支持 --with-infra / -h）" >&2; exit 2 ;;
esac

log() { echo "[dev-down] $*"; }

log "停止 ai-engine / frontend / mobile…"
for name in mobile frontend ai-engine backend; do
  file="${LOG_DIR}/${name}.pgid"
  if [ -f "${file}" ]; then
    pgid="$(cat "${file}")"
    if [ -n "${pgid}" ] && kill -0 "-${pgid}" 2>/dev/null; then
      kill -TERM "-${pgid}" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "-${pgid}" 2>/dev/null || break
        sleep 0.2
      done
      kill -KILL "-${pgid}" 2>/dev/null || true
      log "已停止 ${name}（pgid=${pgid}）"
    else
      log "${name} 未在运行（清理残留 pidfile）"
    fi
    rm -f "${file}" "${LOG_DIR}/${name}.pid"
  fi
done

# 兜底回收：pidfile 之外的残留（[x] 写法避免匹配到本脚本自身）
pkill -f "${ROOT_DIR}/backend-api/.venv/bin/[u]vicorn" 2>/dev/null || true
pkill -f "pa_backend.main:app" 2>/dev/null || true
pkill -f "${ROOT_DIR}/ai-engine/.venv/bin/[p]ython -m src.service" 2>/dev/null || true
pkill -f "${ROOT_DIR}/frontend/node_modules/[.]bin/vite" 2>/dev/null || true
pkill -f "${ROOT_DIR}/mobile-h5/node_modules/[.]bin/vite" 2>/dev/null || true
sleep 1

if [ "${WITH_INFRA}" = "1" ]; then
  log "停止容器（compose 管理的 redis + etcd + minio + milvus；数据卷保留）…"
  (cd "${ROOT_DIR}" && docker compose -f infra/docker-compose.yml down) || true
  # 兜底：手工 `docker run` 起的容器**没有 compose 标签**，compose down 不会认领它们
  # （本仓的 pa-redis 常属这种情况）→ 按容器名显式删除，保证「--with-infra = 容器都停」这一承诺成立。
  for cname in pa-milvus pa-etcd pa-minio pa-redis; do
    if docker container inspect "${cname}" >/dev/null 2>&1; then
      docker rm -f "${cname}" >/dev/null 2>&1 && log "已移除 ${cname}（非 compose 管理，按名字兜底清理）"
    fi
  done
  log "容器已停止（如需清空数据：docker compose -f infra/docker-compose.yml down -v）"
else
  log "容器保留（Redis/Milvus 继续跑；需要停容器请加 --with-infra）"
fi

log "完成。日志保留在 ${LOG_DIR}（后台日志跟进：./scripts/dev-logs.sh）"
