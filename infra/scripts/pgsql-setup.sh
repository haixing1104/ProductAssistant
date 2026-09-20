#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/pgsql-setup.sh
# 用途    : 宿主机本地 PostgreSQL 的初始化 / 权限自检（安全、可反复执行）
#           （数据库不在 Docker；本脚本取代原 docker-entrypoint-initdb.d 的自动执行）
# 用法    : ./infra/scripts/pgsql-setup.sh init       # 一次性引导：建库 + 角色/schema/表 + 授权 + 种子
#           ./infra/scripts/pgsql-setup.sh migrate    # 日常发布用：只应用**未执行过**的迁移文件
#           ./infra/scripts/pgsql-setup.sh verify     # 权限矩阵自检
# 迁移记录: public.schema_migrations(name, applied_at) —— 每个文件**只执行一次**。
#           库是"引入本记录表之前"就初始化过的（记录表刚建、0 条）→ 把现有文件全部登记为
#           **基线**、不重放：0001 的 CREATE ROLE 与 0003 的固定 id INSERT 都不是幂等的，
#           重放必失败（2026-09-20 实测：prod-deploy 第 2 步直接调 init，被 `role_pa_admin
#           已存在` 守卫拒绝，整次发布中止）。
#           → 日常加 schema 变更：新增 database/sql/00NN_*.sql，下一次发布由 migrate 应用一次。
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
# 生产部署下的坑（2026-09 实测）：infra/.env 里 POSTGRES_HOST=host.docker.internal ——
# 那是**容器视角**的宿主别名（容器经它回连宿主 PG）；而本脚本跑在**宿主机**上，
# 该域名在宿主不存在 → pg_isready 直接失败。这里回退到 127.0.0.1（同一个宿主实例）。
# 容器视角只影响应用拼 DSN，不应影响宿主侧管理脚本。
case "$PGHOST" in host.docker.internal) PGHOST=127.0.0.1 ;; esac
PGPORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-productassistant}"
SUPERUSER="${POSTGRES_USER:-postgres}"

# 迁移记录表（放在 public：与业务 schema 的授权矩阵互不影响；只由超级用户读写）
MIGRATIONS_TABLE="public.schema_migrations"

# 超级用户连接：设了 PGPASSWORD 走 TCP，否则本机 postgres OS 用户 peer 认证
psql_super() {
  if [ -n "${PGPASSWORD:-}" ]; then
    psql -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$SUPERUSER" "$@"
  else
    # peer 认证：切到可访问目录，避免 sudo 因当前目录(如 /root)不可读而告警
    ( cd / && sudo -u postgres psql -v ON_ERROR_STOP=1 -p "$PGPORT" "$@" )
  fi
}

# --- 迁移辅助 -----------------------------------------------------------------
pg_has_db()   { psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; }
pg_has_role() { psql_super -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='role_pa_admin'" | grep -q 1; }

# 逐文件灌入（psql 变量目前只有 0001 用到，但统一传参：将来新文件照用不误）
apply_sql_file() {
  psql_super -d "$DB_NAME" \
    -v admin_pwd="$ROLE_PA_ADMIN_PWD" \
    -v backend_pwd="$ROLE_PA_BACKEND_PWD" \
    -v ai_pwd="$ROLE_PA_AI_PWD" \
    -v ai_setup_pwd="$ROLE_PA_AI_SETUP_PWD" < "$1"
}

migrations_table_init() { psql_super -d "$DB_NAME" -c "CREATE TABLE IF NOT EXISTS ${MIGRATIONS_TABLE} (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())" >/dev/null; }
migration_count()    { psql_super -d "$DB_NAME" -tAc "SELECT count(*) FROM ${MIGRATIONS_TABLE}" | tr -d '[:space:]'; }
migration_recorded() { [ "$(psql_super -d "$DB_NAME" -tAc "SELECT count(*) FROM ${MIGRATIONS_TABLE} WHERE name='$1'" | tr -d '[:space:]')" = "1" ]; }
migration_record()   { psql_super -d "$DB_NAME" -c "INSERT INTO ${MIGRATIONS_TABLE}(name) VALUES ('$1') ON CONFLICT DO NOTHING" >/dev/null; }

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
  #（日常发布**不要**用 init，用 migrate：它只补未执行过的文件，见 cmd_migrate）
  if pg_has_role; then
    echo "!! 检测到 role_pa_admin 已存在，库已初始化过。" >&2
    echo "   日常发布请用：./infra/scripts/pgsql-setup.sh migrate（只应用未执行过的迁移）" >&2
    echo "   确需重建请用：infra/scripts/pgsql-reset.sh（需 ALLOW_DESTRUCTIVE=1）" >&2
    exit 1
  fi

  echo "==> 建库（若不存在）：${DB_NAME}"
  pg_has_db || psql_super -d postgres -c "CREATE DATABASE ${DB_NAME}"

  migrations_table_init   # 执行过的文件逐个登记，避免以后 migrate 重放（0001/0003 非幂等）

  echo "==> 按文件名顺序执行 database/sql/*.sql"
  for f in "$SQL_DIR"/*.sql; do
    echo "  - $(basename "$f")"
    # 用 stdin 传入：由调用方(root)读取文件，postgres 用户无需读 /root
    # 角色密码经 psql 变量注入（0001_schema.sql 以 :'admin_pwd' 等引用，兼容 psql 13+）
    apply_sql_file "$f"
    migration_record "$(basename "$f")"
  done

  echo "==> 设置 idle_in_transaction_session_timeout=60s"
  psql_super -d postgres -c "ALTER DATABASE ${DB_NAME} SET idle_in_transaction_session_timeout='60s'"

  echo "==> 初始化完成：$(date '+%F %T')"
}

# 日常发布入口（prod-deploy.sh 第 2 步调它）：
#   · 库未初始化      → 等价于 init（一次性全量引导）
#   · 库已初始化      → 只应用 database/sql/*.sql 里**未登记**的文件（每个一次），
#                       并用 public.schema_migrations 记录，因此可反复执行。
#   · 首次引入记录表  → 库是"历史初始化"的（记录表 0 条）→ 把现有文件全部登记为基线、
#                       不重放（0001 CREATE ROLE / 0003 固定 id INSERT 重放必失败）。
cmd_migrate() {
  preflight

  if ! pg_has_role; then
    echo "==> 未检测到 role_pa_admin（库尚未初始化）→ 走一次性全量引导"
    cmd_init
    return 0
  fi

  migrations_table_init

  if [ "$(migration_count)" = "0" ]; then
    local n_files
    n_files="$(ls -1 "$SQL_DIR"/*.sql | wc -l | tr -d '[:space:]')"
    echo "==> 首次启用迁移记录：把当前 ${n_files} 个文件登记为基线（**不重放**，避免非幂等文件报错）"
    for f in "$SQL_DIR"/*.sql; do
      migration_record "$(basename "$f")"
    done
    echo "==> 基线完成：本次无待应用迁移"
    return 0
  fi

  local pending=0 n
  for f in "$SQL_DIR"/*.sql; do
    n="$(basename "$f")"
    if migration_recorded "$n"; then continue; fi
    echo "  - 应用 $n"
    apply_sql_file "$f"
    migration_record "$n"
    pending=$((pending + 1))
  done

  [ "$pending" -gt 0 ] || echo "==> 无待应用迁移（已是最新）"
  echo "==> 迁移完成：$(date '+%F %T')"
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
  init)    cmd_init ;;
  migrate) cmd_migrate ;;
  verify)  cmd_verify ;;
  *) echo "用法: $0 {init|migrate|verify}" >&2; exit 1 ;;
esac
