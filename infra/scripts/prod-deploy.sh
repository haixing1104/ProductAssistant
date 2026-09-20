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
#                        pgsql-setup.sh migrate：只应用 database/sql 里**未执行过**的文件
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
ENV_FILE="${ROOT_DIR}/infra/.env"
TAG_FILE="${ROOT_DIR}/.deploy-tag"
PREV_TAG_FILE="${ROOT_DIR}/.deploy-tag.prev"
CREDS_FILE="${ROOT_DIR}/.acr-creds"

log()  { echo "[deploy] $*"; }
step() { echo; echo "===== $* ====="; }

# --- 前置检查（失败要给出可执行的下一步，而不是一句 "error"）------------------
[ -f "${ENV_FILE}" ] || {
  echo "!! 缺少 ${ENV_FILE} —— 生产密钥只存在于服务器：见 infra/docs/deploy.md §4" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || { echo "!! 未安装 Docker（先跑 prod-bootstrap.sh）" >&2; exit 1; }

# 把 infra/.env 读进本脚本的环境（与 pgsql-setup.sh / backup-pg.sh **同一写法**）。
#   ⚠️ 修 2026-09-20 实测的 bug：第 1 步要用 PA_IMAGE_REGISTRY 拼 ACR 主机，但此前**没有任何
#   地方**把它送进脚本 —— compose 的 `--env-file` 只作用于 compose 进程本身，CI 也没有导出它，
#   于是第 1 步的守卫 `[ -n "${reg_host}" ]` 直接 exit 1。而死的位置在 `rm -f .acr-creds` 之前，
#   所以症状是「部署步骤 25 秒失败 + .acr-creds 残留 + 从未 docker login 过 + 一个镜像都没拉」，
#   排障时极易被误判成凭据/网络问题（实测凭据是好的：token 端点返回 200）。
set -a; . "${ENV_FILE}"; set +a

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

step "2/5 数据库迁移（forward-only；只应用未执行过的文件，记录在 public.schema_migrations）"
# PGHOST 显式指向宿主回环：生产 .env 的 POSTGRES_HOST 是**容器视角**的 host.docker.internal，
# 而本脚本跑在宿主机上（pgsql-setup.sh/backup-pg.sh 内部也有同样的回退，这里是双保险）。
# ⚠️ 修 2026-09-20 实测：这里原先调的是 `init` —— 那是**一次性引导**脚本，库已初始化时会被
# `role_pa_admin 已存在` 守卫直接 exit 1，于是每次发布都卡在第 2 步（症状：部署步骤几十秒就
# 失败、一个镜像都没拉）。改用 `migrate`：库未初始化时它自动走 init；已初始化时只补
# database/sql 里**未登记**的迁移文件（每个只执行一次），因此可反复执行。
PGHOST=127.0.0.1 ./infra/scripts/pgsql-setup.sh migrate

step "3/5 拉取镜像（tag=${TAG}）"
PA_IMAGE_TAG="${TAG}" "${COMPOSE[@]}" pull

step "4/5 起栈（只重建变化的容器；--wait 等健康检查通过，别跟启动窗口赛跑）"
# --wait + --wait-timeout：等所有服务 healthy（没有 healthcheck 的按 running 计）再往下走。
# 不等也能过，但后面几步就变成跟"容器刚重建、还没热"的窗口赛跑 —— CI 上表现为偶发 502。
PA_IMAGE_TAG="${TAG}" "${COMPOSE[@]}" up -d --wait --wait-timeout 180 --remove-orphans

step "5/5 重载边缘 nginx（重新解析 upstream；否则后端换 IP 后持续 502）"
# ⚠️ `nginx -s reload` 只是给 master **发信号**，重载是**异步**的：发完信号后旧 worker 仍可能把
# 请求打到刚被重建容器的**旧 IP** → 紧接着的验收会拿到 502。2026-09-20 实测复现（CI 那次发布
# 就是这么判红的，而站点其实 3 秒后就全好）：
#   up -d --force-recreate backend-api → exec edge nginx -s reload → prod-verify.sh
#   → ✗ /healthz 502、✗ /readyz 502（其余三项 200）；3 秒后再跑 → 全部通过。
# 因此这里做两件事：① 重载失败**显式报错**（原来 `exec … && log` 会把失败吞成静默跳过，
# 排障时看不到任何线索）；② 验收带**有上限的重试窗口**（见下一步），只有持续失败才判红。
if ! "${COMPOSE[@]}" exec -T edge nginx -s reload; then
  echo "!! edge reload 失败 —— 先在服务器上跑：${COMPOSE[*]} exec -T edge nginx -t（见 docs §10）" >&2
  exit 1
fi
log "edge 已 reload（异步生效；随后的验收会在重试窗口内等它）"

step "验收（走回环 + Host 头验证 nginx→应用全链路；最多 6 次、间隔 5s）"
# 为什么要重试：起栈后容器是"刚重建"的状态，edge 的 upstream 也需要一个重载生效窗口。
# 单次验收撞上窗口期就判红等于把"部署成功"交给了运气 —— 部署脚本必须容忍"刚起来还没热"，
# 但也不能无限等：6 次 × 5s 约 25s 的上限内仍失败，说明确实是坏消息（继续排障 → docs §10）。
verify_ok=0
for attempt in $(seq 1 6); do
  if out="$(./infra/scripts/prod-verify.sh 2>&1)"; then
    printf '%s\n' "${out}"
    verify_ok=1
    break
  fi
  printf '%s\n' "${out}" | grep -E '^✗' || true
  if [ "${attempt}" -lt 6 ]; then
    log "第 ${attempt}/6 次验收未通过（容器或边缘可能仍在预热），5s 后重试"
    sleep 5
  fi
done
[ "${verify_ok}" = "1" ] || {
  echo "!! 验收连续 6 次未通过（约 25s 窗口）—— 排障顺序见 infra/docs/deploy.md §10" >&2
  exit 1
}

# 记录发布锚点：当前 tag 记为 .deploy-tag，上一个成功 tag 记为 .deploy-tag.prev
# （回滚 = 读 .deploy-tag.prev → 用它 up -d；DB 迁移**不回滚**，见 docs §8）
if [ "${MODE}" = "deploy" ] && [ -s "${TAG_FILE}" ] && [ "$(cat "${TAG_FILE}")" != "${TAG}" ]; then
  cp -f "${TAG_FILE}" "${PREV_TAG_FILE}"
fi
printf '%s\n' "${TAG}" > "${TAG_FILE}"
chmod 600 "${TAG_FILE}"
log "发布完成：tag=${TAG}（已写入 ${TAG_FILE}；回滚命令：./infra/scripts/prod-deploy.sh --rollback）"
