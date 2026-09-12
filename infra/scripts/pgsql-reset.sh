#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/pgsql-reset.sh
# 用途    : 重建本地 PostgreSQL —— DROP 数据库与 role_pa_*，再调用 pgsql-setup.sh init
#
# ⚠️ 危险：本脚本会 DESTROY 数据库，且不可逆。仅限开发 / 测试 / CI。
#          严禁对生产库执行；生产请用“备份恢复”或“迁移”，不要 reset。
#
# 用法    : ALLOW_DESTRUCTIVE=1 ./infra/scripts/pgsql-reset.sh -f
#
# 破坏性闸门（必须全部通过才继续）：
#   1) 显式带 -f
#   2) 显式 ALLOW_DESTRUCTIVE=1（默认拒绝）
#   3) ENV / APP_ENV 不能是 prod / production
#   4) 交互输入数据库名确认
#   5) 先 pg_dump 备份到 infra/backups/，失败即中止（不执行 DROP）
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/infra/.env"
SETUP_SH="${ROOT_DIR}/infra/scripts/pgsql-setup.sh"
BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/infra/backups}"

[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE（可从 infra/.env.template 复制）" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

PGHOST="${PGHOST:-${POSTGRES_HOST:-127.0.0.1}}"
PGPORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-productassistant}"
SUPERUSER="${POSTGRES_USER:-postgres}"
ENV_NAME="${ENV:-${APP_ENV:-dev}}"

psql_super() {
  if [ -n "${PGPASSWORD:-}" ]; then
    psql -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$SUPERUSER" "$@"
  else
    ( cd / && sudo -u postgres psql -v ON_ERROR_STOP=1 -p "$PGPORT" "$@" )
  fi
}

pg_dump_super() {   # 结果输出到 stdout
  if [ -n "${PGPASSWORD:-}" ]; then
    pg_dump -h "$PGHOST" -p "$PGPORT" -U "$SUPERUSER" "$@"
  else
    ( cd / && sudo -u postgres pg_dump -p "$PGPORT" "$@" )
  fi
}

# --- 闸门 1：-f ---
[ "${1:-}" = "-f" ] \
  || { echo "!! 需显式 -f：ALLOW_DESTRUCTIVE=1 $0 -f" >&2; exit 1; }

# --- 闸门 2：ALLOW_DESTRUCTIVE=1 ---
[ "${ALLOW_DESTRUCTIVE:-0}" = "1" ] \
  || { echo "!! 拒绝执行：破坏性操作需显式 ALLOW_DESTRUCTIVE=1（默认拒绝）" >&2; exit 1; }

# --- 闸门 3：非生产 ---
case "$(printf '%s' "$ENV_NAME" | tr 'A-Z' 'a-z')" in
  prod|production)
    echo "!! 拒绝执行：当前 ENV=$ENV_NAME 为生产环境，禁止 reset。请用备份恢复或迁移。" >&2
    exit 1 ;;
esac

command -v psql    >/dev/null 2>&1 || { echo "未找到 psql" >&2; exit 1; }
command -v pg_dump >/dev/null 2>&1 || { echo "未找到 pg_dump" >&2; exit 1; }

# --- 闸门 4：交互确认（输入数据库名）---
printf '将 DROP 数据库 "%s" 及其角色*，此操作不可逆。\n请输入数据库名以确认: ' "$DB_NAME" >&2
read -r CONFIRM
[ "$CONFIRM" = "$DB_NAME" ] || { echo "!! 确认失败（输入与库名不符），已中止。" >&2; exit 1; }

# --- 闸门 5：先备份，失败即中止 ---
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql"
echo "==> 先备份到 ${BACKUP_FILE}"
if ! pg_dump_super -d "$DB_NAME" > "$BACKUP_FILE"; then
  rm -f "$BACKUP_FILE"
  echo "!! 备份失败，已中止，未执行任何 DROP。" >&2
  exit 1
fi
echo "==> 备份完成（$(du -h "$BACKUP_FILE" | cut -f1)）"

# --- 执行破坏性操作 ---
echo "==> DROP DATABASE IF EXISTS ${DB_NAME}"
psql_super -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)"
echo "==> DROP ROLE IF EXISTS role_pa_*"
psql_super -d postgres -c "DROP ROLE IF EXISTS role_pa_admin, role_pa_backend, role_pa_ai, role_pa_ai_setup"

echo "==> 调用 pgsql-setup.sh init 重建"
"$SETUP_SH" init

echo "==> reset 完成：$(date '+%F %T')"
