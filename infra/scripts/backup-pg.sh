#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/backup-pg.sh
# 用途 : PostgreSQL 逻辑备份（pg_dump 自定义格式）+ 旧备份轮转
# 用法 : ./infra/scripts/backup-pg.sh [保留份数]        # 默认 7 份
#        建议用 root 的 crontab 每天 3:10 跑（**本脚本不自动装 cron**，理由见下）：
#          sudo crontab -e
#          10 3 * * * /srv/pa/infra/scripts/backup-pg.sh >/var/log/pa-backup.log 2>&1
# 产出 : infra/backups/pa-YYYYmmdd-HHMMSS.dump（已在 .gitignore 中排除，绝不入库）
# 为什么用自定义格式(-Fc)：压缩 + 支持 pg_restore 选择性恢复（比纯 SQL 文本实用得多）
# 为什么不自动装 cron：改别人的定时任务是"替用户做决定"，出问题时最难排查的也是它 ——
#         所以脚本只做"一次正确备份"，调度权留给运维（一行 crontab 即可）。
# 注意 : 备份 ≠ 可恢复。真正出事时是"恢复到某台新机器"，所以**定期演练**：
#        从 dump 恢复到临时库并跑一次 `pgsql-setup.sh verify`。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/infra/.env"
BACKUP_DIR="${ROOT_DIR}/infra/backups"
KEEP="${1:-7}"

[ -f "${ENV_FILE}" ] || { echo "!! 缺少 ${ENV_FILE}" >&2; exit 1; }
set -a; . "${ENV_FILE}"; set +a

DB_NAME="${POSTGRES_DB:-productassistant}"
PGHOST_="${PGHOST:-${POSTGRES_HOST:-127.0.0.1}}"
# 与 pgsql-setup.sh 同一处坑：生产 .env 的 POSTGRES_HOST 是**容器视角**的
# host.docker.internal，而本脚本跑在宿主机上 → 回退 127.0.0.1。
case "${PGHOST_}" in host.docker.internal) PGHOST_=127.0.0.1 ;; esac
PGPORT_="${POSTGRES_PORT:-5432}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BACKUP_DIR}/pa-${STAMP}.dump"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

# 连接方式与 pgsql-setup.sh 同口径：设了 PGPASSWORD 走 TCP，否则用 postgres 系统用户的 peer 认证
if [ -n "${PGPASSWORD:-}" ]; then
  dump_cmd() { pg_dump -h "${PGHOST_}" -p "${PGPORT_}" -U "${POSTGRES_USER:-postgres}" -Fc -f "$@" "${DB_NAME}"; }
else
  dump_cmd() { ( cd / && sudo -u postgres pg_dump -p "${PGPORT_}" -Fc -f "$@" "${DB_NAME}" ); }
fi

echo "[backup] 备份 ${DB_NAME} → ${OUT}"
dump_cmd "${OUT}"
chmod 600 "${OUT}"
echo "[backup] 完成：$(du -h "${OUT}" | cut -f1)"

# 轮转：按文件名（= 时间戳）倒序保留最新 KEEP 份，其余删除
if [ "${KEEP}" -gt 0 ]; then
  # shellcheck disable=SC2012
  ls -1t "${BACKUP_DIR}"/pa-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "[backup] 轮转删除：$(basename "${old}")"
    rm -f "${old}"
  done
fi

echo "[backup] 当前保留：$(ls -1 "${BACKUP_DIR}"/pa-*.dump 2>/dev/null | wc -l) 份"
echo "[backup] 提示：请把 ${BACKUP_DIR} 异地备份（同机备份挡不住磁盘故障）"
