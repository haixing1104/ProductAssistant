#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/check-cert-expiry.sh
# 用途 : 检查边缘 nginx 证书的**剩余有效期**与**是否自签** —— 防两类线上事故：
#        · 阿里云免费证书只有 **90 天**：忘了换 = 某天浏览器集体告警（且无任何征兆）；
#        · prod-bootstrap.sh 的自签兜底证书长期挂着 = 一直「不安全」（10 年有效期，
#          靠"到期"永远发现不了它，所以本脚本也把「自签」当作异常报出来）。
# 用法 : ./infra/scripts/check-cert-expiry.sh [--warn-days 21] [--certs-dir DIR] [--quiet]
# 退出码: 0 = 正常（正式证书且剩余 ≥ 下限）
#        1 = 证书缺失 / 已过期 / 剩余不足 / 是自签（调用方据此打 ::warning:: 或告警）
# 调用 : prod-deploy.sh 在验收后跑一次（只告警、**不阻断发布**）；也可手工或 cron 跑。
# 说明 : 只读脚本 —— 只解析证书的公钥信息，**绝不打印私钥内容**。
# 文档 : infra/docs/deploy.md §7.5
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERT_DIR="${ROOT_DIR}/infra/nginx/certs"
WARN_DAYS=21
QUIET=0

usage() {
  cat <<'EOF'
用法: ./infra/scripts/check-cert-expiry.sh [选项]

  --warn-days <n>   剩余多少天起算「快到期」，默认 21
  --certs-dir <d>   证书目录，默认 <仓库>/infra/nginx/certs
  --quiet           正常时不打印（只输出问题）
  -h, --help        显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --warn-days) WARN_DAYS="${2:-}"; [ -n "$WARN_DAYS" ] || { echo "!! --warn-days 需要参数" >&2; exit 2; }; shift 2 ;;
    --certs-dir) CERT_DIR="${2:-}"; [ -n "$CERT_DIR" ] || { echo "!! --certs-dir 需要参数" >&2; exit 2; }; shift 2 ;;
    --quiet)     QUIET=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; echo "!! 未知参数：$1" >&2; exit 2 ;;
  esac
done

CERT="${CERT_DIR}/fullchain.pem"
if [ ! -s "$CERT" ]; then
  echo "!! [cert] 未找到证书 ${CERT}"
  echo "   → edge 的 443 需要它才能启动；首次部署见 docs §7.1（bootstrap 自签兜底），换正式证书见 §7.5"
  exit 1
fi

SUBJ="$(openssl x509 -in "$CERT" -noout -subject 2>/dev/null | sed 's/^subject=//')" \
  || { echo "!! [cert] 无法解析 ${CERT}（不是 PEM x509？）"; exit 1; }
ISSUER="$(openssl x509 -in "$CERT" -noout -issuer 2>/dev/null | sed 's/^issuer=//')"
END_RAW="$(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)"
DAYS_LEFT=$(( ($(date -d "$END_RAW" +%s) - $(date +%s)) / 86400 ))

STATUS=0
echo "[cert] 主体=${SUBJ} 签发者=${ISSUER} 有效期至=${END_RAW}（剩余 ${DAYS_LEFT} 天）"

if [ "$SUBJ" = "$ISSUER" ]; then
  echo "!! [cert] 当前是**自签证书**（issuer == subject）：浏览器会提示「不安全」，"
  echo "        钉钉/微信内置浏览器可能直接拒绝 → 换正式证书：docs §7.5（阿里云免费证书，90 天）"
  STATUS=1
fi

if [ "$DAYS_LEFT" -lt 0 ]; then
  echo "!! [cert] 证书**已过期**（${END_RAW}）：立刻换证 —— docs §7.5"
  STATUS=1
elif [ "$DAYS_LEFT" -lt "$WARN_DAYS" ]; then
  echo "!! [cert] 剩余 ${DAYS_LEFT} 天 < ${WARN_DAYS} 天：到阿里云控制台重新下载 → 用"
  echo "        ./infra/scripts/install-cert.sh <证书链.pem> <私钥.key> 安装（docs §7.5）"
  STATUS=1
fi

if [ "$STATUS" = "0" ] && [ "$QUIET" = "0" ]; then
  echo "✓ [cert] 证书正常（正式证书，剩余 ${DAYS_LEFT} 天 ≥ ${WARN_DAYS} 天）"
fi
exit "$STATUS"
