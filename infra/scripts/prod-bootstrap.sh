#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/prod-bootstrap.sh
# 用途 : **服务器一次性初始化**（幂等，可反复执行）—— 把一台干净的 Ubuntu 22.04
#        变成能跑 infra/docker-compose.prod.yml 的机器：
#          S1 swap 追加到 2GB · S2 Docker（阿里云源 + 加速器 + 日志上限）
#          S3 PostgreSQL（含"容器可达"这一必改项）· S4 目录与自签证书兜底 · S5 汇总
# 用法 : sudo ./infra/scripts/prod-bootstrap.sh
#        （由 .github/workflows/deploy.yml 的 mode=bootstrap 经 SSH 调用，也可手工执行）
# 前置 : 以 **root** 运行（apt/systemctl/写 /etc 都需要）；网络可达阿里云内网源
# 不做什么 :
#   · 不碰 infra/.env（密钥文件只存在于服务器，绝不进仓库/CI 日志）；
#   · 不启用 ufw —— 边界由**阿里云安全组**承担。Docker 发布端口时会自己写 iptables
#     的 DOCKER 链，**绕过 ufw**，所以 ufw 只会给人"已经防火了"的错觉（见 docs §1）；
#   · 不装 Milvus 组（2C2G 内存装不下，走 compose 的 rag profile，见 docs §6）。
# 实测依据 : Ubuntu 22.04.5 / 2 vCPU / 1608MB 内存 / 40G 系统盘 / 已有 1GB swap(/www/swap)
#            / apt 走 mirrors.cloud.aliyuncs.com / registry-1.docker.io 直连超时。
# 完整步骤与排障：infra/docs/deploy.md
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STEP=0
log()  { echo "[bootstrap] $*"; }
step() { STEP=$((STEP + 1)); echo; echo "===== S${STEP}. $* ====="; }

# 只用于给自签证书起 CN（读不到 .env 也没关系：certbot 之后会覆盖同名文件）
DOMAIN="${PA_DOMAIN:-}"
if [ -z "${DOMAIN}" ] && [ -f "${ROOT_DIR}/infra/.env" ]; then
  DOMAIN="$(grep -E '^PA_DOMAIN=' "${ROOT_DIR}/infra/.env" | head -1 | cut -d= -f2- || true)"
fi
DOMAIN="${DOMAIN:-localhost}"

[ "$(id -u)" = "0" ] || { echo "!! 必须以 root 运行（或 sudo）" >&2; exit 1; }
log "目标：$(. /etc/os-release && echo "${PRETTY_NAME}") · $(nproc) 核 · $(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)MB 内存"

# ---------------------------------------------------------------------------
# S1 swap：目标 2GB
#   为什么需要：2C2G 上同时跑 PG + 两个 Python 服务 + Redis，内存余量只有几百 MB；
#   一旦突发（CSV 导入、批量生成）就越过 OOM-kill 线，容器被杀得莫名其妙。
#   本节刻意**只追加**：机器上已有 /www/swap（1GB，且在 /etc/fstab 里）——
#   改写既有条目有把系统启动搞挂的风险，所以不动它，缺多少补到 /swapfile。
# ---------------------------------------------------------------------------
step "swap 检查与追加（目标 2048MB）"
current_swap_mb="$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)"
if [ "${current_swap_mb}" -ge 2048 ]; then
  log "swap 已足够：${current_swap_mb}MB（跳过）"
else
  need_mb=$((2048 - current_swap_mb))
  if [ -f /swapfile ]; then
    log "/swapfile 已存在，跳过创建（如需扩容请手工处理）"
  else
    log "创建 /swapfile（${need_mb}MB）"
    # fallocate 在部分文件系统上不可靠 → 失败退回 dd（慢但稳）
    fallocate -l "${need_mb}M" /swapfile 2>/dev/null \
      || dd if=/dev/zero of=/swapfile bs=1M count="${need_mb}" status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  # 降低换出倾向：swap 是"防猝死"的兜底，不该被日常使用（否则磁盘 IO 拖慢接口）
  sysctl -w vm.swappiness=10 >/dev/null
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-pa-swappiness.conf
  log "swap 现为：$(awk '/SwapTotal/{printf "%dMB", $2/1024}' /proc/meminfo)"
fi

# ---------------------------------------------------------------------------
# S2 Docker（docker-ce + compose 插件）
#   · 走**阿里云 docker-ce 源**（实测 200，国内快）；
#   · registry-mirrors 必须有：本机实测 registry-1.docker.io **直连超时**——
#     生产应用镜像全部来自 ACR，但第三方基础镜像（如 redis）仍需要它；
#   · log-opts 上限 10MB×3：40G 系统盘上不能让容器日志无界增长（默认 json-file 无上限）。
# ---------------------------------------------------------------------------
step "Docker 安装与加固"
if command -v docker >/dev/null 2>&1; then
  log "Docker 已安装：$(docker --version)"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  log "已安装：$(docker --version) / docker compose $(docker compose version --short)"
fi
systemctl enable --now docker

if [ ! -f /etc/docker/daemon.json ]; then
  log "写入 /etc/docker/daemon.json（镜像加速 + 日志轮转）"
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'JSON'
{
  "registry-mirrors": ["https://docker.1ms.run", "https://dockerproxy.net"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
  systemctl restart docker
else
  log "/etc/docker/daemon.json 已存在，保持不动"
fi
docker info >/dev/null 2>&1 || { echo "!! Docker 未就绪（systemctl status docker）" >&2; exit 1; }

# ---------------------------------------------------------------------------
# S3 PostgreSQL（宿主实例）
#   · 用 Ubuntu 22.04 **自带版本**（README 实测口径：本仓库就跑在 22.04 自带的 14.24 上），
#     不加 PGDG 源 —— 少一次跨境 apt 环节；
#   · ⚠️ **必改项**：默认只监听 127.0.0.1，而本项目的应用跑在**容器**里（仓库原则：
#     pgsql 在本地层、应用在容器层）→ 容器经 host.docker.internal 连宿主会被**直接拒绝**。
#     这里放开监听 + 在 pg_hba 里**只放行 Docker 私有网段**（172.16.0.0/12）。
#     安全性：该网段在云上不可路由，且安全组不放行 5432 → 公网到不了；
#     这比"监听 0.0.0.0 + 放行 0.0.0.0/0"安全得多。
# ---------------------------------------------------------------------------
step "PostgreSQL 安装与容器可达性配置"
HBA_CHANGED=0
if command -v psql >/dev/null 2>&1; then
  log "PostgreSQL 已安装：$(psql --version)"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq postgresql postgresql-client
fi
systemctl enable --now postgresql

pg_conf_dir="$(ls -d /etc/postgresql/*/main 2>/dev/null | head -1 || true)"
[ -n "${pg_conf_dir}" ] || { echo "!! 未找到 PostgreSQL 配置目录（/etc/postgresql/*/main）" >&2; exit 1; }

if grep -qE "^listen_addresses\s*=\s*'\*'" "${pg_conf_dir}/postgresql.conf"; then
  log "listen_addresses 已是 '*'（跳过）"
else
  sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "${pg_conf_dir}/postgresql.conf"
  log "listen_addresses → '*'（容器需要经网桥回连宿主）"
  HBA_CHANGED=1
fi

if grep -q 'pa-docker-bridge' "${pg_conf_dir}/pg_hba.conf"; then
  log "pg_hba 已含 Docker 网段放行（跳过）"
else
  cat >> "${pg_conf_dir}/pg_hba.conf" <<'HBA'

# pa-docker-bridge: 应用容器（backend-api / ai-engine）经 host.docker.internal 连宿主 PG。
# 只放行 Docker 私有网段 + scram-sha-256（PG14 默认口令加密），不放行 0.0.0.0/0。
host    all             all             172.16.0.0/12           scram-sha-256
HBA
  log "pg_hba → 追加 172.16.0.0/12 + scram-sha-256"
  HBA_CHANGED=1
fi

if [ "${HBA_CHANGED}" = "1" ]; then
  systemctl restart postgresql
fi

pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 \
  || { echo "!! PostgreSQL 未就绪（systemctl status postgresql）" >&2; exit 1; }
log "PostgreSQL 就绪：$(sudo -u postgres psql -tAc 'SELECT version();' | cut -d, -f1)"

# ---------------------------------------------------------------------------
# S4 目录 + 自签证书兜底 + rsync
#   为什么需要自签证书：边缘 nginx 的 443 server **必须在启动时**能读到
#   /etc/nginx/certs/{fullchain,privkey}.pem，否则直接启动失败 —— 而正式证书要靠
#   certbot 在我们的 nginx 起来之后（HTTP-01 webroot）才能签发。
#   所以先自签把栈跑起来，再签正式证书覆盖同名文件 + reload（见 docs §7）。
# ---------------------------------------------------------------------------
step "部署目录、证书兜底与 rsync"
CERT_DIR="${ROOT_DIR}/infra/nginx/certs"
mkdir -p /srv/pa/infra/nginx/certs /srv/pa/infra/nginx/www "${CERT_DIR}"
if [ -s "${CERT_DIR}/fullchain.pem" ] && [ -s "${CERT_DIR}/privkey.pem" ]; then
  log "证书已存在：${CERT_DIR}（保持不动；certbot 签发的正式证书也放这里）"
else
  log "生成自签证书兜底（CN=${DOMAIN}，10 年）"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "${CERT_DIR}/privkey.pem" -out "${CERT_DIR}/fullchain.pem" \
    -subj "/CN=${DOMAIN}" >/dev/null 2>&1
  chmod 600 "${CERT_DIR}/privkey.pem"
  log "自签证书就绪（浏览器会告警 → 签正式证书后消失）"
fi

if command -v rsync >/dev/null 2>&1; then
  log "rsync 已安装：$(rsync --version | head -1)"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq rsync
  log "rsync 已安装：$(rsync --version | head -1)"
fi

# ---------------------------------------------------------------------------
# S5 汇总（只报状态，不含任何密钥值）
# ---------------------------------------------------------------------------
step "汇总"
cat <<EOF
  OS            : $(. /etc/os-release && echo "${PRETTY_NAME}")
  CPU / 内存     : $(nproc) 核 / $(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)MB（可用 $(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo)MB）
  swap          : $(swapon --show --noheadings | awk '{printf "%s(%s) ", $1, $3}')
  磁盘          : $(df -h / | awk 'NR==2{print $4" 可用 / "$2}')
  Docker        : $(docker --version)
  Compose       : docker compose $(docker compose version --short)
  PostgreSQL    : $(psql --version)
  部署目录       : /srv/pa
  证书目录       : ${CERT_DIR}
  infra/.env    : $([ -f "${ROOT_DIR}/infra/.env" ] && echo '已存在 → 可执行 mode=deploy' || echo '**尚未创建** → 见 docs/deploy.md §4')
EOF
log "完成。下一步：确认 infra/.env 就绪 → 触发 deploy.yml 的 mode=deploy"

