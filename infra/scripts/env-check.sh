#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/env-check.sh
# 用途    : 自检「代码 / compose 读取的键」与「infra/.env.template 登记」与
#           「infra/.env 实配」三方是否一致，并提示弱口令占位未替换。
# 用法    : ./infra/scripts/env-check.sh           # 日常自检（弱口令仅告警）
#           ./infra/scripts/env-check.sh --prod    # 生产上线前自检（弱口令也阻断）
# 判定口径（分级，只有 ERROR 影响退出码）:
#   [ERROR] 模板未注释键（active）在 .env 中缺失
#   [ERROR] --prod 下：密钥类键仍是占位 / 弱口令 / 空值
#   [WARN ] 代码或 compose 读取、但模板完全没有登记的键（须回填模板；豁免名单除外）
#   [WARN ] .env 有、模板未登记的键（未纳入契约，请回填或说明）
#   [WARN ] 非 --prod 下的占位/弱口令（本地合理留空的值不在此列，见下）
#   [INFO ] .env 中密钥类键为空（仅 --prod 计 ERROR：OSS_ENDPOINT / REDIS_PASSWORD
#           这类键本地就该留空，日常模式不提示，避免噪音淹掉真问题）
# 豁免名单（不参与缺键/回填判定）:
#   PA_TEST_*  = 测试专属覆盖（tests / *-test compose）
#   PGHOST/PGPORT = 由 POSTGRES_* 派生（脚本与测试内自带默认值）
#   LANGGRAPH_STRICT_MSGPACK = 代码 setdefault 注入的库内加固，非用户配置项
# 注意    : 只输出键名与原因，绝不打印键值（避免密钥进入日志）。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="${ROOT_DIR}/infra/.env.template"
ENV_FILE="${ROOT_DIR}/infra/.env"

usage() {
  cat <<'EOF'
用法: ./infra/scripts/env-check.sh [--prod]

  （无参数）  日常自检：缺键为 ERROR；弱口令/未登记键为 WARN（不影响退出码）
  --prod      生产上线前自检：弱口令/占位/空值同样计为 ERROR
  -h, --help  显示本帮助
EOF
}

PROD_MODE=0
case "${1:-}" in
  "")        ;;
  --prod)    PROD_MODE=1 ;;
  -h|--help) usage; exit 0 ;;
  *)         echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
esac

[ -f "$TEMPLATE" ] || { echo "!! 缺少模板 $TEMPLATE" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "!! 缺少 $ENV_FILE（先跑 ./infra/scripts/env-init.sh）" >&2; exit 1; }

# 豁免名单：见头部注释（新增豁免必须写明理由）
EXEMPT="PA_TEST_LLM PA_TEST_PG_DSN PA_TEST_PG_SETUP_PG_DSN PGHOST PGPORT LANGGRAPH_STRICT_MSGPACK"

# 密钥类键（值不应是占位/弱口令/空）与弱口令启发式
# ROOT_USER 单列：MinIO 的 root 用户与口令同值（minioadminADMIN）也是生产大忌，但不牵连 POSTGRES_USER
SECRET_RE='PASSWORD|_PWD|SECRET|API_KEY|ACCESS_KEY_ID|TOKEN|WEBHOOK_URL|ROOT_USER'
WEAK_RE='^(CHANGE_ME|change_me|changeme|change-me|password|postgres|minioadmin|minioadminADMIN|dev-only-change-me)$|^dev[-_].*$|.*_dev$'

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 三方键集合 ---
# 提取键名：按第一个 `"` 切分再删尾引号
# （不能用 `s/.*"//`：贪婪匹配会一路吃到行尾引号，把整行清空 → 键集恒为空）
{
  grep -rhoE 'os\.getenv\([[:space:]]*"[A-Z0-9_]+"' "${ROOT_DIR}/ai-engine/src" --include='*.py' || true
  grep -rhoE 'os\.environ\.get\([[:space:]]*"[A-Z0-9_]+"' "${ROOT_DIR}/ai-engine/src" --include='*.py' || true
  grep -rhoE 'os\.environ\.setdefault\([[:space:]]*"[A-Z0-9_]+"' "${ROOT_DIR}/ai-engine/src" --include='*.py' || true
  grep -rhoE 'os\.environ\[[[:space:]]*"[A-Z0-9_]+"' "${ROOT_DIR}/ai-engine/src" --include='*.py' || true
} | sed -E 's/^[^"]*"//; s/"$//' | sort -u > "$TMP_DIR/code"

grep -rhoE '\$\{[A-Z0-9_]+' "${ROOT_DIR}"/infra/docker-compose*.yml \
  | sed -E 's/^\$\{//' | sort -u > "$TMP_DIR/compose"

grep -E '^[A-Z][A-Z0-9_]+=' "$TEMPLATE" | sed -E 's/=.*//' | sort -u > "$TMP_DIR/tpl_active"
# 注释形式的可选键：`# KEY=值`（值不含空白），避免把 `# KEY=xxx 时…` 这类说明文字误判为键
grep -E '^#[[:space:]]*[A-Z][A-Z0-9_]+=[^[:space:]]*$' "$TEMPLATE" | sed -E 's/^#[[:space:]]*//; s/=.*//' | sort -u > "$TMP_DIR/tpl_optional"
cat "$TMP_DIR/tpl_active" "$TMP_DIR/tpl_optional" | sort -u > "$TMP_DIR/tpl_all"

grep -E '^[[:space:]]*[A-Z][A-Z0-9_]+=' "$ENV_FILE" | sed -E 's/^[[:space:]]*//; s/=.*//' | sort -u > "$TMP_DIR/env_active"

printf '%s\n' $EXEMPT | sort -u > "$TMP_DIR/exempt"

ERRORS=0
WARNS=0

echo "==> env-check（$([ "$PROD_MODE" = "1" ] && echo '生产模式 --prod' || echo '日常模式')）"
echo "    模板 active=$(wc -l < "$TMP_DIR/tpl_active" | tr -d ' ')  optional=$(wc -l < "$TMP_DIR/tpl_optional" | tr -d ' ')" \
     " .env=$(wc -l < "$TMP_DIR/env_active" | tr -d ' ')" \
     " 代码=$(wc -l < "$TMP_DIR/code" | tr -d ' ')  compose=$(wc -l < "$TMP_DIR/compose" | tr -d ' ')"
echo

# --- 检查 1：模板 active 必须在 .env 中 ---
comm -23 "$TMP_DIR/tpl_active" "$TMP_DIR/env_active" > "$TMP_DIR/missing"
if [ -s "$TMP_DIR/missing" ]; then
  echo "!! [ERROR] 模板已登记但 .env 缺失的键（$(wc -l < "$TMP_DIR/missing" | tr -d ' ') 个）："
  sed 's/^/       /' "$TMP_DIR/missing"
  echo "       → 修复：./infra/scripts/env-init.sh"
  ERRORS=$((ERRORS + 1))
fi

# --- 检查 2：代码/compose 读取但模板未登记（豁免除外）---
cat "$TMP_DIR/code" "$TMP_DIR/compose" | sort -u > "$TMP_DIR/consumers"
comm -23 "$TMP_DIR/consumers" "$TMP_DIR/tpl_all" | comm -23 - "$TMP_DIR/exempt" > "$TMP_DIR/unregistered"
if [ -s "$TMP_DIR/unregistered" ]; then
  echo "-- [WARN] 代码/compose 读取但模板未登记的键（$(wc -l < "$TMP_DIR/unregistered" | tr -d ' ') 个）："
  sed 's/^/       /' "$TMP_DIR/unregistered"
  echo "       → 修复：在 infra/.env.template 登记（含默认值 + 注释），再跑 env-init.sh"
  WARNS=$((WARNS + 1))
fi

# --- 检查 3：.env 有但模板未登记 ---
comm -23 "$TMP_DIR/env_active" "$TMP_DIR/tpl_all" > "$TMP_DIR/extra"
if [ -s "$TMP_DIR/extra" ]; then
  echo "-- [WARN] .env 中未纳入模板契约的键（$(wc -l < "$TMP_DIR/extra" | tr -d ' ') 个）："
  sed 's/^/       /' "$TMP_DIR/extra"
  echo "       → 若属共享配置，请回填模板；若为本机专属，请在模板注释中说明"
  WARNS=$((WARNS + 1))
fi

# --- 检查 4：密钥类键的占位/弱口令（空值仅 --prod 计 ERROR）---
: > "$TMP_DIR/weak"
: > "$TMP_DIR/empty"
# 剥引号用字面量：写成变量，避免在参数展开的模式里被反斜杠吃掉（"${val%\'}" 实际模式是「反斜杠+单引号」）
DQ='"'; SQ="'"
while IFS= read -r line; do
  case "$line" in
    ''|'#'*) continue ;;
  esac
  key="${line%%=*}"
  val="${line#*=}"
  val="${val#$DQ}"; val="${val%$DQ}"
  val="${val#$SQ}"; val="${val%$SQ}"
  # 非密钥类键跳过（注意用 if，不用 `A && B`：后者在 set -e 下作为循环体末尾返回非 0 会中断脚本）
  if ! echo "$key" | grep -qE "$SECRET_RE"; then
    continue
  fi
  if [ -z "$val" ]; then
    # 空值分两档：prod 必须填（ERROR 级），日常模式只做 INFO（REDIS_PASSWORD/OSS_ENDPOINT 本地就该空）
    if [ "$PROD_MODE" = "1" ]; then
      printf '%s\n' "$key" >> "$TMP_DIR/weak"
    else
      printf '%s\n' "$key" >> "$TMP_DIR/empty"
    fi
    continue
  fi
  if printf '%s' "$val" | grep -qE "$WEAK_RE"; then
    printf '%s\n' "$key" >> "$TMP_DIR/weak"
  fi
done < "$ENV_FILE"

if [ -s "$TMP_DIR/weak" ]; then
  if [ "$PROD_MODE" = "1" ]; then
    echo "!! [ERROR] 密钥类键仍是占位/弱口令/空值（$(wc -l < "$TMP_DIR/weak" | tr -d ' ') 个）："
    ERRORS=$((ERRORS + 1))
  else
    echo "-- [WARN] 密钥类键仍是占位/弱口令（$(wc -l < "$TMP_DIR/weak" | tr -d ' ') 个）："
    WARNS=$((WARNS + 1))
  fi
  sed 's/^/       /' "$TMP_DIR/weak"
  echo "       → 生产上线前必须替换（值不在本报告中打印）"
fi

if [ -s "$TMP_DIR/empty" ]; then
  echo "   [信息] 密钥类键当前为空（本地合理；--prod 会升级为 ERROR）："
  sed 's/^/       /' "$TMP_DIR/empty"
fi

# --- 豁免清单：显式列出，避免"静默豁免" ---
if [ -s "$TMP_DIR/exempt" ]; then
  echo "   [信息] 豁免（测试专属 / 派生 / 库内加固，见脚本头注释）："
  sed 's/^/       /' "$TMP_DIR/exempt"
fi

echo
if [ "$ERRORS" -gt 0 ]; then
  echo "!! env-check 失败：ERROR ${ERRORS} 项，WARN ${WARNS} 项"
  exit 1
fi
echo "==> env-check 通过：ERROR 0 项，WARN ${WARNS} 项"
