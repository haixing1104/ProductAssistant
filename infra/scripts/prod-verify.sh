#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/prod-verify.sh
# 用途 : 部署后**验收**（在服务器上执行）—— 走本机回环 + Host 头，验证
#        边缘 nginx → 静态容器/后端 的完整链路，而不是只看"容器起来了"。
# 用法 : ./infra/scripts/prod-verify.sh [域名]      # 默认取 $PA_DOMAIN 或 seektruth.org.cn
#        退出码 0 = 全部通过；非 0 = 至少一项失败（CI 里据此判红）
# 判据 :
#   /healthz  200       后端进程活着（**不查依赖**：查依赖会让 PG 抖动演变成容器反复重启）
#   /readyz   200       后端就绪（DB SELECT 1 + Redis PING 都通；不通返回 503）
#   /         200 + id="root"   PC 工作台 SPA 壳
#   /m/       200 + id="root"   移动端 H5（验证"前缀剥离"这条链路真的通）
#   /welcome/ 200 + id="root"   作品集宣传页（同上）
# 为什么在服务器上跑而不是从 CI 直接打域名 :
#   CI runner 在海外，到国内公网的链路本身就是变量 —— 部署成功与否不该被它掩盖；
#   回环 + Host 头已经能验证 nginx 路由、反代与三个静态容器，是**因果最近**的判据。
# 注意 : 用 -k 跳过自签证书校验（首次部署用自签证书兜底，见 prod-bootstrap.sh）。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOMAIN="${1:-${PA_DOMAIN:-seektruth.org.cn}}"
BASE="https://127.0.0.1"
FAILED=0

check() {
  local name="$1" url="$2" expect="$3" pattern="${4:-}"
  local out code body
  # -s 静默、-k 跳证书、-m 10s 超时；-w 把状态码打在最后一行便于切分
  out="$(curl -sk -m 10 -w '\n%{http_code}' -H "Host: ${DOMAIN}" "${url}" || true)"
  code="$(printf '%s' "${out}" | tail -n1)"
  body="$(printf '%s' "${out}" | sed '$d')"

  if [ "${code}" != "${expect}" ]; then
    echo "✗ ${name} → HTTP ${code}（期望 ${expect}）  ${url}"
    FAILED=1
    return 0
  fi
  if [ -n "${pattern}" ] && ! printf '%s' "${body}" | grep -q "${pattern}"; then
    echo "✗ ${name} → 200 但内容不含 ${pattern}   ${url}"
    FAILED=1
    return 0
  fi
  echo "✓ ${name} → ${code}   ${url}"
}

echo "[verify] 目标：${BASE}（Host: ${DOMAIN}）"

# 先看后端：它的两类探针语义不同，必须分开断言（见文件头判据）
check "后端存活 /healthz"   "${BASE}/healthz"    200
check "后端就绪 /readyz"    "${BASE}/readyz"     200
# 再看三个前端：/m/ 与 /welcome/ 能 200 且拿到 SPA 壳，说明"前缀剥离 + SPA 回退"都对
check "PC 工作台 /"         "${BASE}/"           200 'id="root"'
check "移动端 H5 /m/"       "${BASE}/m/"         200 'id="root"'
check "宣传页 /welcome/"    "${BASE}/welcome/"   200 'id="root"'

if [ "${FAILED}" = "1" ]; then
  echo "[verify] ✗ 验收未通过 —— 排障顺序：compose ps → 容器日志 → edge nginx -t"
  exit 1
fi
echo "[verify] ✓ 全部通过"
