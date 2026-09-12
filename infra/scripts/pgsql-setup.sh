#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/pgsql-setup.sh
# 用途    : 宿主机本地 PostgreSQL 的初始化 / 权限自检（安全、可反复执行）
#           （数据库不在 Docker；本脚本取代原 docker-entrypoint-initdb.d 的自动执行）
# 用法    : ./infra/scripts/pgsql-setup.sh init       # 建库 + 角色/schema/表 + 授权 + 种子
#           ./infra/scripts/pgsql-setup.sh verify     # 权限矩阵自检
# 备注    : 重建（DROP + init）属破坏性操作，已拆到独立脚本：
#             infra/scripts/pgsql-reset.sh（仅限非生产，需 ALLOW_DESTRUCTIVE=1）
# 依赖    : psql >= 13；本机 PostgreSQL 已启动；infra/.env 已就绪
# 连接    : 默认 sudo -u postgres peer 认证；设 PGPASSWORD 则走 TCP（POSTGRES_HOST/PORT）
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/infra/.env"
SQL_DIR="${ROOT_DIR}/database/sql"

[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE（可从 infra/.env.template 复制）" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

PGHOST="${PGHOST:-${POSTGRES_HOST:-127.0.0.1}}"
PGPORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-productassistant}"
SUPERUSER="${POSTGRES_USER:-postgres}"

# 超级用户连接：设了 PGPASSWORD 走 TCP，否则本机 postgres OS 用户 peer 认证
psql_super() {
  if [ -n "${PGPASSWORD:-}" ]; then
    psql -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$SUPERUSER" "$@"
  else
    # peer 认证：切到可访问目录，避免 sudo 因当前目录(如 /root)不可读而告警
    ( cd / && sudo -u postgres psql -v ON_ERROR_STOP=1 -p "$PGPORT" "$@" )
  fi
}

preflight() {
  if ! command -v psql >/dev/null 2>&1; then
    cat >&2 <<'EOF'
!! 未找到 psql（PostgreSQL 客户端）。请先安装 PostgreSQL（>= 13，建议 17）：

  Ubuntu / Debian:
    sudo apt-get update
    sudo apt-get install -y postgresql-client-17     # 17 需先加 PGDG 源；或用发行版自带版本
  RHEL / 阿里云 Alibaba Cloud Linux:
    sudo dnf install -y postgresql17
  macOS (Homebrew):
    brew install postgresql@17

  安装完成后重试：./infra/scripts/pgsql-setup.sh init
EOF
    exit 1
  fi

  if ! pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1; then
    cat >&2 <<EOF
!! PostgreSQL 未就绪：$PGHOST:$PGPORT。请先启动本机 PostgreSQL 服务：

  Ubuntu / Debian:
    sudo systemctl start postgresql          # 或 sudo service postgresql start
  RHEL / 阿里云:
    sudo systemctl start postgresql-17       # 服务名随安装方式而定
  确认监听：
    ss -ltn | grep $PGPORT

  说明：若数据库不在本机，请在 infra/.env 设置正确的 POSTGRES_HOST / POSTGRES_PORT 后重试。
EOF
    exit 1
  fi
}

cmd_init() {
  preflight

  # 0001_schema.sql 的 CREATE ROLE 非幂等：已初始化过就明确报错，避免误改
  if psql_super -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='role_pa_admin'" | grep -q 1; then
    echo "!! 检测到 role_pa_admin 已存在，库已初始化过。如需重建请执行：infra/scripts/pgsql-reset.sh（需 ALLOW_DESTRUCTIVE=1）" >&2
    exit 1
  fi

  echo "==> 建库（若不存在）：${DB_NAME}"
  psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
    || psql_super -d postgres -c "CREATE DATABASE ${DB_NAME}"

  echo "==> 按文件名顺序执行 database/sql/*.sql"
  for f in "$SQL_DIR"/*.sql; do
    echo "  - $(basename "$f")"
    # 用 stdin 传入：由调用方(root)读取文件，postgres 用户无需读 /root
    # 角色密码经 psql 变量注入（0001_schema.sql 以 :'admin_pwd' 等引用，兼容 psql 13+）
    psql_super -d "$DB_NAME" \
      -v admin_pwd="$ROLE_PA_ADMIN_PWD" \
      -v backend_pwd="$ROLE_PA_BACKEND_PWD" \
      -v ai_pwd="$ROLE_PA_AI_PWD" \
      -v ai_setup_pwd="$ROLE_PA_AI_SETUP_PWD" < "$f"
  done

  echo "==> 设置 idle_in_transaction_session_timeout=60s"
  psql_super -d postgres -c "ALTER DATABASE ${DB_NAME} SET idle_in_transaction_session_timeout='60s'"

  echo "==> 初始化完成：$(date '+%F %T')"
}

cmd_verify() {
  local dsn="postgresql://role_pa_ai@${PGHOST}:${PGPORT}/${DB_NAME}"

  echo "==> role_pa_ai 读 schema_pa_backend.products（应返回 count）"
  PGPASSWORD="$ROLE_PA_AI_PWD" psql "$dsn" -c "SELECT count(*) FROM schema_pa_backend.products;"

  echo "==> role_pa_ai 写 schema_pa_backend.products（应 permission denied）"
  if PGPASSWORD="$ROLE_PA_AI_PWD" psql "$dsn" \
       -c "UPDATE schema_pa_backend.products SET base_price=1;" 2>/dev/null; then
    echo "!! 异常：role_pa_ai 不应有写权限" >&2
    exit 1
  else
    echo "OK：已按预期拒绝写入"
  fi
}

case "${1:-}" in
  init)   cmd_init ;;
  verify) cmd_verify ;;
  *) echo "用法: $0 {init|verify}" >&2; exit 1 ;;
esac
