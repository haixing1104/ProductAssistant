#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/install-cert.sh
# 用途 : 把**已签发的正式证书**（阿里云免费证书 / certbot / 其他 CA）装上边缘 nginx ——
#        「校验 → 备份 → 原子安装 → nginx -t → reload」一次跑完，任一步失败**自动回滚**。
# 用法 : ./infra/scripts/install-cert.sh <证书链.pem> <私钥.key>
#          # 服务器上（root）；通常先 scp 到 /tmp：
#          ./infra/scripts/install-cert.sh /tmp/new-fullchain.pem /tmp/new-privkey.pem
# 可选 : --domain <d>      期望覆盖的域名（默认取 infra/.env 的 PA_DOMAIN，再回退 seektruth.org.cn）
#        --min-days <n>    证书剩余有效期下限，默认 7（快过期的证书不装进线上）
#        --certs-dir <d>   证书目录，默认 <仓库>/infra/nginx/certs（自测用）
#        --dry-run         只校验，不写任何文件、不碰 nginx
#        --no-reload       安装但不执行 nginx -t / reload（自测用）
# 为什么要有这个脚本（而不是手工两条 install 命令）:
#   ① 证书是「错了就全站红」的文件，而**三种典型错误手工装时当场看不出来**：
#      pem 与 key 不配对 / 少了中间证书 / SAN 不含实际访问的域名（如 www）——
#      它们只在**某些客户端**上表现为「打不开」。所以本脚本先把这四件事验掉，再动线上文件。
#      顺带识别「自签」（issuer == subject）：那正是「浏览器提示不安全」的根因。
#   ② 安装用**同目录 mv -f**（rename 原子）而不是 cp 覆盖：nginx 永远读不到「半截文件」。
#   ③ 备份 + 自动回滚：nginx -t 或 reload 失败就把旧证书原样放回并再验一次 ——
#      避免「为了修证书反而把站点弄挂」。
# 前置 : edge 读的是 infra/nginx/certs/{fullchain,privkey}.pem（见 infra/nginx/conf.d/pa.conf）；
#        宿主 ./nginx/certs 只读挂进容器 —— 所以**只需替换这两个文件**，不重启容器。
# 注意 : ① 私钥副本用完请删（rm -f /tmp/new-privkey.pem）；
#        ② 证书目录已在 .gitignore 与 CI 的 rsync 排除项里 → 发布不会覆盖证书，
#           因此**换证必须手工跑本脚本**（到期时间见 infra/scripts/check-cert-expiry.sh）；
#        ③ 依赖 GNU date（Ubuntu 22.04 服务器口径），本脚本面向服务器侧运行。
# 文档 : infra/docs/deploy.md §7.5（阿里云免费证书换证 SOP）
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/infra/.env"
COMPOSE=(docker compose --env-file infra/.env -f infra/docker-compose.prod.yml)

CERT_DIR="${ROOT_DIR}/infra/nginx/certs"
DOMAIN=""
MIN_DAYS=7
DRY_RUN=0
DO_RELOAD=1

log() { echo "[cert] $*"; }
ok()  { echo "✓ $*"; }
die() { echo "!! $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: ./infra/scripts/install-cert.sh <证书链.pem> <私钥.key> [选项]

  <证书链.pem>   Nginx 版证书包的 <域名>.pem（应含服务器证书 + 中间 CA）
  <私钥.key>     Nginx 版证书包的 <域名>.key

选项:
  --domain <d>      期望覆盖的域名（默认 infra/.env 的 PA_DOMAIN → seektruth.org.cn）
  --min-days <n>    剩余有效期下限，默认 7 天
  --certs-dir <d>   证书目录，默认 <仓库>/infra/nginx/certs
  --dry-run         只校验，不写文件
  --no-reload       只安装，不执行 nginx -t / reload
  -h, --help        显示本帮助
EOF
}

PEM=""
KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain)    DOMAIN="${2:-}"; [ -n "$DOMAIN" ] || die "--domain 需要参数"; shift 2 ;;
    --min-days)  MIN_DAYS="${2:-}"; [ -n "$MIN_DAYS" ] || die "--min-days 需要参数"; shift 2 ;;
    --certs-dir) CERT_DIR="${2:-}"; [ -n "$CERT_DIR" ] || die "--certs-dir 需要参数"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --no-reload) DO_RELOAD=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    -*)          usage >&2; die "未知参数：$1" ;;
    *)           if [ -z "$PEM" ]; then PEM="$1"; elif [ -z "$KEY" ]; then KEY="$1"; else die "多余参数：$1"; fi; shift ;;
  esac
done
if [ -z "$PEM" ] || [ -z "$KEY" ]; then
  usage >&2
  exit 2
fi

# --- 域名：未指定时从 infra/.env 取 PA_DOMAIN（剥掉可能存在的引号与空格）------------
if [ -z "$DOMAIN" ] && [ -f "$ENV_FILE" ]; then
  DOMAIN="$(grep -E '^PA_DOMAIN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
fi
DOMAIN="${DOMAIN:-seektruth.org.cn}"

# --- ① 输入可解析性 -----------------------------------------------------------
command -v openssl >/dev/null 2>&1 || die "未安装 openssl（apt-get install -y openssl）"
[ -s "$PEM" ] || die "证书文件不存在或为空：$PEM"
[ -s "$KEY" ] || die "私钥文件不存在或为空：$KEY"
# 加密私钥 nginx 用不了；且 openssl 会等口令输入（非交互环境下「挂住」比报错更难查）
if grep -q 'ENCRYPTED' "$KEY"; then
  die "私钥带口令（ENCRYPTED）：nginx 不支持，先解密 —— openssl pkey -in $KEY -out /tmp/plain.key"
fi
[ -n "$(openssl x509 -in "$PEM" -noout -subject 2>/dev/null)" ] \
  || die "无法解析证书（$PEM）：不是 PEM 格式的 x509 证书（IIS 的 .pfx 请先转 PEM）"

SUBJ="$(openssl x509 -in "$PEM" -noout -subject | sed 's/^subject=//')"
ISSUER="$(openssl x509 -in "$PEM" -noout -issuer | sed 's/^issuer=//')"
END_RAW="$(openssl x509 -in "$PEM" -noout -enddate | cut -d= -f2)"
END_EPOCH="$(date -d "$END_RAW" +%s)"
NOW_EPOCH="$(date +%s)"
DAYS_LEFT=$(( (END_EPOCH - NOW_EPOCH) / 86400 ))
CHAIN_N="$(grep -c 'BEGIN CERTIFICATE' "$PEM" || true)"
log "主体：${SUBJ}"
log "签发者：${ISSUER}"
log "有效期至：${END_RAW}（剩余 ${DAYS_LEFT} 天）"
log "证书链：${CHAIN_N} 张"

# --- ② 有效期 ----------------------------------------------------------------
[ "$DAYS_LEFT" -gt 0 ] || die "证书已过期（${END_RAW}）—— 不要把它装进线上"
[ "$DAYS_LEFT" -ge "$MIN_DAYS" ] \
  || die "证书剩余 ${DAYS_LEFT} 天 < 下限 ${MIN_DAYS} 天：请先申请/下载新证书（--min-days 可放宽下限）"
[ "$CHAIN_N" -ge 2 ] \
  || echo "-- [WARN] 链里只有 1 张证书：很可能缺中间证书，部分客户端（老 Android / 微信内置浏览器）会报链不完整" >&2

# --- ③ 域名覆盖（SAN 优先；无 SAN 时回退 CN；支持 *.example.com 一层通配符）---------
SANS_RAW="$(openssl x509 -in "$PEM" -noout -ext subjectAltName 2>/dev/null \
  | tr ', ' '\n\n' | sed -n 's/^DNS:\(.*\)$/\1/p' | sed '/^$/d')"
if [ -z "$SANS_RAW" ]; then
  SANS_RAW="$(printf '%s' "$SUBJ" | sed -n 's/.*CN *= *//p')"
  echo "-- [WARN] 证书没有 subjectAltName（SAN）→ 回退用 CN 判断；现代浏览器（Chrome≥58）忽略 CN，会报证书名不符" >&2
fi
name_matches() {  # $1=期望域名  $2=证书里的名字；返回 0 = 匹配
  local want="$1" have="$2" suffix prefix
  [ "$want" = "$have" ] && return 0
  case "$have" in
    \*.*)
      suffix="${have#\*.}"
      case "$want" in
        *."$suffix")
          prefix="${want%."$suffix"}"
          case "$prefix" in *.*) return 1 ;; *) return 0 ;; esac ;;
        *) return 1 ;;
      esac ;;
    *) return 1 ;;
  esac
}
matched=0
while IFS= read -r n; do
  [ -n "$n" ] || continue
  if name_matches "$DOMAIN" "$n"; then matched=1; break; fi
done <<< "$SANS_RAW"
[ "$matched" = "1" ] \
  || die "证书未覆盖域名 ${DOMAIN}（证书里的名字：$(printf '%s ' $SANS_RAW)）—— 例如要用 www，就得让 SAN 里含 www.域名"

# --- ④ 证书与私钥配对（公钥比对，不打印任何密钥内容）------------------------------
PEM_PUB="$(openssl x509 -in "$PEM" -noout -pubkey)"
KEY_PUB="$(openssl pkey -in "$KEY" -pubout 2>/dev/null || true)"
[ -n "$KEY_PUB" ] || die "无法解析私钥（$KEY）：不是 PEM 格式的未加密私钥？"
[ "$PEM_PUB" = "$KEY_PUB" ] || die "证书与私钥**不配对**（公钥不一致）—— 装上去 nginx 会直接 [emerg]"
ok "四项校验通过：覆盖 ${DOMAIN} ✓ 密钥配对 ✓ 链 ${CHAIN_N} 张 ✓ 剩余 ${DAYS_LEFT} 天 ✓"
if [ "$SUBJ" = "$ISSUER" ]; then
  echo "-- [WARN] 这是一张**自签证书**（issuer == subject）：浏览器仍会提示「不安全」" >&2
fi

# --- ⑤ 安装（--dry-run 到此为止）------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  log "--dry-run：校验完毕，未写任何文件"
  exit 0
fi

mkdir -p "$CERT_DIR"
# 备份目录名带 PID：同一秒内连续跑两次（脚本化换证时很常见）不会互相覆盖备份目录
STAMP="$(date +%Y%m%d-%H%M%S)-$$"
BACKUP_DIR="${CERT_DIR}/backup-${STAMP}"
HAD_OLD=0
if [ -s "${CERT_DIR}/fullchain.pem" ] && [ -s "${CERT_DIR}/privkey.pem" ]; then
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  cp -a "${CERT_DIR}/fullchain.pem" "${BACKUP_DIR}/fullchain.pem"
  cp -a "${CERT_DIR}/privkey.pem"   "${BACKUP_DIR}/privkey.pem"
  HAD_OLD=1
  log "已备份现有证书 → ${BACKUP_DIR}"
else
  echo "-- [WARN] ${CERT_DIR} 下没有现成证书（首次安装？）：若失败将无备份可回滚" >&2
fi

# 原子安装：先写同目录临时文件再 mv -f（rename 是原子操作 → nginx 读不到半截文件）
install_atomic() {  # $1=源文件  $2=目标路径  $3=权限
  local src="$1" dst="$2" mode="$3"
  # 临时名带 PID：并发/同秒连跑时不会互相踩到对方的半成品
  cat "$src" > "${dst}.new.$$"
  chmod "$mode" "${dst}.new.$$"
  mv -f "${dst}.new.$$" "$dst"
}
install_atomic "$PEM" "${CERT_DIR}/fullchain.pem" 644
install_atomic "$KEY" "${CERT_DIR}/privkey.pem"   600
ok "已安装：${CERT_DIR}/{fullchain,privkey}.pem"

rollback() {
  if [ "$HAD_OLD" = "1" ]; then
    echo "!! 回滚：恢复备份 ${BACKUP_DIR}" >&2
    cp -a "${BACKUP_DIR}/fullchain.pem" "${CERT_DIR}/fullchain.pem"
    cp -a "${BACKUP_DIR}/privkey.pem"   "${CERT_DIR}/privkey.pem"
  else
    echo "!! 没有旧证书可回滚：请手工恢复（例如重跑 prod-bootstrap.sh 生成自签兜底）" >&2
  fi
}

# --- ⑥ nginx -t + reload（只 reload，绝不 restart：restart 遇证书问题 = 全站 502）---
if [ "$DO_RELOAD" = "0" ]; then
  log "--no-reload：跳过 nginx -t / reload（本次仅安装）"
else
  command -v docker >/dev/null 2>&1 || { rollback; die "未安装 docker —— 无法验证配置（见 docs §3）"; }
  cd "$ROOT_DIR"
  if ! "${COMPOSE[@]}" exec -T edge nginx -t; then
    rollback
    if "${COMPOSE[@]}" exec -T edge nginx -t >/dev/null 2>&1; then
      echo "!! 新证书未通过 nginx -t，已回滚：旧证书仍在线" >&2
    else
      echo "!! 回滚后 nginx -t 仍失败：请立刻人工介入 —— ${COMPOSE[*]} exec -T edge nginx -t" >&2
    fi
    exit 1
  fi
  ok "nginx -t 通过"
  if ! "${COMPOSE[@]}" exec -T edge nginx -s reload; then
    rollback
    die "edge reload 失败（已回滚证书）—— 排查：${COMPOSE[*]} exec -T edge nginx -t"
  fi
  ok "edge 已 reload（零停机；容器未重建）"
fi

# --- ⑦ 备份轮转：只留最近 3 份（目录已在 .gitignore / rsync 排除项内，但别无限堆积）--
# shellcheck disable=SC2012
ls -1dt "${CERT_DIR}"/backup-* 2>/dev/null | tail -n +4 | while IFS= read -r old; do
  log "清理旧备份：$(basename "$old")"
  rm -rf -- "$old"
done || true

cat <<EOF

==> 完成。验收：
    echo | openssl s_client -connect ${DOMAIN}:443 -servername ${DOMAIN} 2>/dev/null | openssl x509 -noout -subject -issuer -dates
    curl -sSI https://${DOMAIN}/ | head -1
    ./infra/scripts/prod-verify.sh          # 端到端 5/5 ✓
    ./infra/scripts/check-cert-expiry.sh    # 到期前 21 天起告警
    · 浏览器：地址栏应出现「锁」；钉钉/微信内置浏览器可正常打开
    · 清理私钥副本：rm -f /tmp/new-privkey.pem
    · 换证 SOP 与到期提醒：infra/docs/deploy.md §7.5
EOF
