#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/prod-deploy.sh
# 用途 : **服务器侧发布脚本**（在服务器上执行；由 deploy.yml 的 mode=deploy 调用，
#        也可手工执行）—— 一次跑完：迁移 → 拉镜像 → 起栈 → 重载边缘 → 验收。
# 用法 : ./infra/scripts/prod-deploy.sh <镜像tag>      # 正常发布（tag 一般 = git sha）
#        ./infra/scripts/prod-deploy.sh --rollback     # 回滚到上一次成功发布的 tag
# 前置 : infra/.env 已就绪（密钥只存在于服务器）；docker compose 已安装（见 prod-bootstrap.sh）
# 幂等 : 可反复执行。迁移脚本 forward-only 且幂等（见 database/sql 与 docs §5）。
#
# 五步的**顺序即语义**（每一步都在解决一个具体的坑）:
#   1) ACR 登录      —— 若 .acr-creds 存在（CI 经 stdin 写入）就用它登录并**立即删除**；
#   2) 数据库迁移     —— 必须在应用起来之前（新代码可能依赖新列/新表）；
#   3) pull           —— 先把镜像取全再切换，避免"起了一半才发现镜像拉不到"；
#   4) up -d          —— 只重建变化的容器；--remove-orphans 清掉已下线的服务；
#   5) edge reload    —— ⚠️ 关键一步：nginx 把 upstream 的解析结果**缓存在内存**，
#                        而第 4 步会把 backend 容器**重建并换新 IP** → 不 reload 就一直 502；
#                        这里 reload 会重新解析域名（零停机）。
# 最后 : prod-verify.sh 做端到端验收（不通过则退出码非 0）+ 写 .deploy-tag 作为回滚锚点。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE=(docker compose --env-file infra/.env -f infra/docker-compose.prod.yml)
TAG_FILE="${ROOT_DIR}/.deploy-tag"
PREV_TAG_FILE="${ROOT_DIR}/.deploy-tag.prev"
CREDS_FILE="${ROOT_DIR}/.acr-creds"

log()  { echo "[deploy] $*"; }
step() { echo; echo "===== $* ====="; }

# --- 前置检查（失败要给出可执行的下一步，而不是一句 "error"）------------------
[ -f "${ROOT_DIR}/infra/.env" ] || {
  echo "!! 缺少 infra/.env —— 生产密钥只存在于服务器：见 infra/docs/deploy.md §4" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || { echo "!! 未安装 Docker（先跑 prod-bootstrap.sh）" >&2; exit 1; }

# --- 解析参数：正常发布或回滚 --------------------------------------------------
MODE="deploy"
if [ "${1:-}" = "--rollback" ]; then
  MODE="rollback"
  [ -s "${PREV_TAG_FILE}" ] || { echo "!! 没有可回滚的上一版本（${PREV_TAG_FILE} 不存在）" >&2; exit 1; }
  TAG="$(cat "${PREV_TAG_FILE}")"
  log "回滚模式：目标 tag = ${TAG}（来自 ${PREV_TAG_FILE}）"
else
  TAG="${1:-}"
  [ -n "${TAG}" ] || { echo "用法: $0 <镜像tag> | --rollback" >&2; exit 1; }
  log "发布模式：目标 tag = ${TAG}"
fi

step "1/5 ACR 登录"
if [ -s "${CREDS_FILE}" ]; then
  # CI 把凭据经 stdin 写成 600 权限的临时文件（不出现在进程命令行里），用完即删
  # shellcheck disable=SC1090
  set -a; . "${CREDS_FILE}"; set +a
  reg_host="${PA_IMAGE_REGISTRY:-}"; reg_host="${reg_host%%/*}"
  [ -n "${reg_host}" ] || { echo "!! infra/.env 缺少 PA_IMAGE_REGISTRY" >&2; exit 1; }
  echo "${ACR_PASSWORD}" | docker login -u "${ACR_USERNAME}" --password-stdin "${reg_host}" >/dev/null
  rm -f "${CREDS_FILE}"
  log "已登录 ${reg_host}（凭据文件已删除）"
else
  # 没有临时凭据：依赖 ~/.docker/config.json 里既有的登录（首次 bootstrap 时可手工登录一次）
  log "无 ${CREDS_FILE}：沿用 docker 已保存的凭据（若拉取报 unauthorized，见 docs §10）"
fi

step "2/5 数据库迁移（幂等，forward-only）"
# PGHOST 显式指向宿主回环：生产 .env 的 POSTGRES_HOST 是**容器视角**的 host.docker.internal，
# 而本脚本跑在宿主机上（pgsql-setup.sh/backup-pg.sh 内部也有同样的回退，这里是双保险）。
PGHOST=127.0.0.1 ./infra/scripts/pgsql-setup.sh init

step "3/5 拉取镜像（tag=${TAG}）"
PA_IMAGE_TAG="${TAG}" "${COMPOSE[@]}" pull

step "4/5 起栈（只重建变化的容器）"
PA_IMAGE_TAG="${TAG}" "${COMPOSE[@]}" up -d --remove-orphans

step "5/5 重载边缘 nginx（重新解析 upstream；否则后端换 IP 后持续 502）"
"${COMPOSE[@]}" exec -T edge nginx -s reload && log "edge 已 reload"

step "验收（走回环 + Host 头，验证 nginx→应用全链路）"
./infra/scripts/prod-verify.sh

# 记录发布锚点：当前 tag 记为 .deploy-tag，上一个成功 tag 记为 .deploy-tag.prev
# （回滚 = 读 .deploy-tag.prev → 用它 up -d；DB 迁移**不回滚**，见 docs §8）
if [ "${MODE}" = "deploy" ] && [ -s "${TAG_FILE}" ] && [ "$(cat "${TAG_FILE}")" != "${TAG}" ]; then
  cp -f "${TAG_FILE}" "${PREV_TAG_FILE}"
fi
printf '%s\n' "${TAG}" > "${TAG_FILE}"
chmod 600 "${TAG_FILE}"
log "发布完成：tag=${TAG}（已写入 ${TAG_FILE}；回滚命令：./infra/scripts/prod-deploy.sh --rollback）"
