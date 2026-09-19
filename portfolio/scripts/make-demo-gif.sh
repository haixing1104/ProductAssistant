#!/usr/bin/env bash
#
# make-demo-gif.sh —— 作品集演示素材的压缩流水线（把实测参数固化，免得下次重新摸索）
#
# 依赖：ffmpeg（编解码）+ gifsicle（GIF 无损优化/裁帧）。缺任一都只影响用到它的子命令。
#
# 用法：
#   scripts/make-demo-gif.sh lossless <in.gif> <out.gif>
#   scripts/make-demo-gif.sh trim     <in.gif> <from_frame> <to_frame> <out.gif>
#   scripts/make-demo-gif.sh gif      <raw_video> <out.gif>
#   scripts/make-demo-gif.sh mp4      <raw_video> <out.mp4>
#   scripts/make-demo-gif.sh poster   <video> <out.webp>
#   scripts/make-demo-gif.sh info     <file>
#
# 环境变量（gif / mp4 / poster 用，默认值就是本仓落盘时用的那套）：
#   SS=40     起始秒（跳过开头：加载白屏、登录、无意义等待）
#   DUR=30    时长秒（一段 = 一个动作；动图不能暂停，超过 35s 访客只会看开头）
#   W=240     输出宽   FPS=6    帧率   COLORS=64  调色板颜色数（仅 GIF）   CRF=30（仅 mp4）
#
# 实测（详见 public/demos/pa/README.md，别按"经验值"估）：
#   * gifsicle -O3 --careful 对**已经优化过**的 GIF 是 0% —— 无损已经到顶；
#     对未优化的导出件只有 3% 左右（16.41M → 15.87M）。
#   * 真要小：① 裁短/裁帧（无损）② 降 W/FPS/COLORS（有损）③ 改用 mp4（小 6–8×，还能暂停）。
#   * 同一段 35s 素材：mp4 360px CRF30 = 0.52M；GIF 240px/6fps/64c = 3.21M、280px/8fps/64c = 4.41M。
#   * 屏上文字多 → dither=none 比默认抖动更小也更清晰；diff_mode=rectangle 让每帧只编码变化区域。
#
set -euo pipefail

SS="${SS:-40}"
DUR="${DUR:-30}"
W="${W:-240}"
FPS="${FPS:-6}"
COLORS="${COLORS:-64}"
CRF="${CRF:-30}"

die() { echo "错误：$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "缺少 $1（本机装一下：apt install $2）"; }
usage() { awk '/^set -euo/{exit} NR>1{print}' "$0" | sed 's/^# \{0,1\}//'; }

# 无损：只重排编码（帧间差分 + 局部色表），像素零变化。已是下限时输出≈输入，属正常。
cmd_lossless() {
  [ $# -eq 2 ] || die "用法：lossless <in.gif> <out.gif>"
  need gifsicle gifsicle
  local before after
  before=$(stat -c%s "$1")
  gifsicle -O3 --careful "$1" -o "$2"
  after=$(stat -c%s "$2")
  awk -v a="$before" -v b="$after" 'BEGIN{printf "无损优化：%.2fM → %.2fM（省 %.1f%%）\n", a/1048576, b/1048576, (a>b)?(a-b)*100/a:0}'
}

# 无损裁帧：帧号从 0 起。帧号 ÷ 帧率 = 秒（用 info 看帧率），体积随帧数近似线性下降。
# 注意：gifsicle 选帧用「帧区间参数」（`'#0-119'`），**没有** `--from/--to` 这种开关。
cmd_trim() {
  [ $# -eq 4 ] || die "用法：trim <in.gif> <from_frame> <to_frame> <out.gif>"
  need gifsicle gifsicle
  gifsicle "$1" "#$2-$3" -O3 --careful -o "$4"
  echo "已裁剪第 $2–$3 帧 → $4"
  cmd_info "$4"
}

# 录屏 → GIF：两遍调色板（palettegen 采样整段 → paletteuse 逐帧上色）
cmd_gif() {
  [ $# -eq 2 ] || die "用法：gif <raw_video> <out.gif>"
  need ffmpeg ffmpeg
  ffmpeg -v error -y -ss "$SS" -t "$DUR" -i "$1" \
    -vf "fps=${FPS},scale=${W}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=${COLORS}:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    -loop 0 "$2"
  if command -v gifsicle >/dev/null 2>&1; then
    gifsicle -O3 --careful "$2" -o "$2.tmp" && mv "$2.tmp" "$2"
  fi
  cmd_info "$2"
}

# 录屏 → mp4：体积最小且能暂停。+faststart 必须留（否则要下完整段才起播）；-an 去音轨。
cmd_mp4() {
  [ $# -eq 2 ] || die "用法：mp4 <raw_video> <out.mp4>"
  need ffmpeg ffmpeg
  ffmpeg -v error -y -ss "$SS" -t "$DUR" -i "$1" -vf "scale=${W}:-2:flags=lanczos" -an \
    -c:v libx264 -crf "$CRF" -preset slow -pix_fmt yuv420p -movflags +faststart "$2"
  cmd_info "$2"
}

# 首帧海报：给 <video> 用（GIF 不需要，动图首帧自己会画）
cmd_poster() {
  [ $# -eq 2 ] || die "用法：poster <video> <out.webp>"
  need ffmpeg ffmpeg
  ffmpeg -v error -y -i "$1" -vf "select=eq(n\,0)" -frames:v 1 "$2"
  echo "首帧海报 → $2"
}

# 量尺寸与时长：数据里的 `aspect`、`duration` 都从这里来，别凭手感写
cmd_info() {
  [ $# -eq 1 ] || die "用法：info <file>"
  need ffprobe ffprobe
  local w h dur frames="" raw=""
  w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$1")
  h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$1")
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$1")
  if [ "${1##*.}" = "gif" ] && command -v gifsicle >/dev/null 2>&1; then
    # 先把 gifsicle 的输出整段读进来再解析：直接 `gifsicle --info | head` 会在 pipefail 下
    # 因 SIGPIPE 判处 141（帧多了必现），把好好的流水线弄成"失败"。
    raw=$(gifsicle --info "$1")
    frames=$(printf '%s\n' "$raw" | awk 'NR==1 && match($0, /[0-9]+ images/) {print substr($0, RSTART, RLENGTH-7)}')
  fi
  awk -v f="$1" -v w="$w" -v h="$h" -v d="$dur" -v n="$frames" -v b="$(stat -c%s "$1")" \
    'BEGIN{printf "%-52s %sx%s  %.1fs  %.2fM%s\n", f, w, h, d, b/1048576, (n==""?"":"  " n " 帧  " sprintf("%.1f", n/d) " fps")}'
}

[ $# -ge 2 ] || { usage; exit 1; }
cmd="$1"; shift
case "$cmd" in
  lossless|trim|gif|mp4|poster|info) "cmd_$cmd" "$@" ;;
  -h|--help|help) usage ;;
  *) usage; die "未知子命令：$cmd" ;;
esac
