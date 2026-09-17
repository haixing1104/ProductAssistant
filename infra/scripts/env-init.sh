#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/env-init.sh
# 用途    : 让 infra/.env 与 infra/.env.template（入库契约，见其头部注释）保持一致
#           · .env 不存在  → 由模板生成一份（含 CHANGE_ME 占位符）并 chmod 600
#           · .env 已存在  → 只追加「模板未注释但 .env 缺失」的键，绝不覆盖已有值
# 用法    : ./infra/scripts/env-init.sh              # 补齐（幂等，可反复执行）
#           ./infra/scripts/env-init.sh --dry-run    # 只打印将追加的内容，不写文件
# 判定口径:
#           · 模板里「已注释」的键 = 可选覆盖项（如两个 DSN），不参与补齐判定；
#           · .env 里的注释行不算已配置 —— `set -a; source .env` 不会加载它；
#           · 只追加、不删除、不改写：脚本可安全重复执行，也不会动你的真值。
# 依赖    : bash 4+；无其它依赖。改完请跑 ./infra/scripts/env-check.sh 自检。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="${ROOT_DIR}/infra/.env.template"
ENV_FILE="${ROOT_DIR}/infra/.env"

usage() {
  cat <<'EOF'
用法: ./infra/scripts/env-init.sh [--dry-run]

  （无参数）   把 infra/.env 补齐到模板未注释键的全集（幂等；不覆盖已有值）
  --dry-run    只打印将要追加的键与内容，不修改任何文件
  -h, --help   显示本帮助
EOF
}

DRY_RUN=0
case "${1:-}" in
  "")            ;;
  --dry-run)     DRY_RUN=1 ;;
  -h|--help)     usage; exit 0 ;;
  *)             echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
esac

[ -f "$TEMPLATE" ] || { echo "缺少模板 $TEMPLATE" >&2; exit 1; }

# --- 1) .env 不存在：整体由模板生成（一次性 bootstrap）---
if [ ! -f "$ENV_FILE" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] 将执行: cp $TEMPLATE $ENV_FILE && chmod 600 $ENV_FILE"
    exit 0
  fi
  cp "$TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "==> 已由模板生成 $ENV_FILE（权限 600）"
  echo "    下一步：替换全部 CHANGE_ME 占位符，并核对 PA_ENV / NOTIFY_* / OSS_* / ZHIPU_API_KEY。"
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 2) 求差集：模板未注释键 − .env 未注释键 ---
grep -E '^[A-Z][A-Z0-9_]+=' "$TEMPLATE" \
  | sed -E 's/=.*//' | sort -u > "$TMP_DIR/tpl_active"
grep -E '^[[:space:]]*[A-Z][A-Z0-9_]+=' "$ENV_FILE" \
  | sed -E 's/^[[:space:]]*//; s/=.*//' | sort -u > "$TMP_DIR/env_active"
comm -23 "$TMP_DIR/tpl_active" "$TMP_DIR/env_active" > "$TMP_DIR/missing"

TPL_COUNT="$(wc -l < "$TMP_DIR/tpl_active" | tr -d ' ')"
if [ ! -s "$TMP_DIR/missing" ]; then
  echo "==> 无需补齐：.env 已覆盖模板全部未注释键（${TPL_COUNT} 个）"
  exit 0
fi

MISS_COUNT="$(wc -l < "$TMP_DIR/missing" | tr -d ' ')"
echo "==> 模板已登记而 .env 缺失的键（${MISS_COUNT} 个）："
sed 's/^/    + /' "$TMP_DIR/missing"

PERMS="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')"
[ "$PERMS" = "600" ] \
  || echo "!! 提示：$ENV_FILE 权限为 $PERMS（内含真实密钥，建议 chmod 600）" >&2

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "[dry-run] 将追加到 $ENV_FILE 的内容（不覆盖任何已有值）："
  while IFS= read -r key; do
    grep -m1 -E "^${key}=" "$TEMPLATE" | sed 's/^/    /'
  done < "$TMP_DIR/missing"
  exit 0
fi

# --- 3) 追加（带来源注释头，便于追溯与回滚）---
{
  echo
  echo "# --- 由 infra/scripts/env-init.sh 于 $(date '+%F %T') 从 infra/.env.template 补齐 ---"
  echo "#（仅追加缺失键；已有值一律保持原样；默认值可直接运行，密钥类占位符需替换为真值）"
  while IFS= read -r key; do
    grep -m1 -E "^${key}=" "$TEMPLATE"
  done < "$TMP_DIR/missing"
} >> "$ENV_FILE"

echo "==> 已追加 ${MISS_COUNT} 个键到 $ENV_FILE"

# --- 4) 自检：补齐后必须覆盖模板全部未注释键 ---
grep -E '^[[:space:]]*[A-Z][A-Z0-9_]+=' "$ENV_FILE" \
  | sed -E 's/^[[:space:]]*//; s/=.*//' | sort -u > "$TMP_DIR/env_after"
if comm -23 "$TMP_DIR/tpl_active" "$TMP_DIR/env_after" | grep -q .; then
  echo "!! 自检失败：仍缺少以下键（请检查模板格式）：" >&2
  comm -23 "$TMP_DIR/tpl_active" "$TMP_DIR/env_after" >&2
  exit 1
fi
echo "==> 自检通过：.env 已覆盖模板全部未注释键（${TPL_COUNT} 个）"
echo "    下一步：./infra/scripts/env-check.sh（生产上线前加 --prod）"
