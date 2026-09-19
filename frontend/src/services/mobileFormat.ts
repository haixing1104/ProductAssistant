// 移动端展示工具（纯函数，便于单测）—— **H5 与 RN 共用同一份**。
//
// 位置说明（2026-09 从 `mobile-h5/src/services/format.ts` 上移到契约核心层）:
//   出现第 2 个移动端消费方（`mobile-rn`）后，这些格式化/颜色翻译口径必须**同源**，
//   否则两端会出现"同一个分数在 H5 是金色、在 RN 是灰色"这类看不见的语义漂移。
//   覆盖率口径：用例随文件一起放在共享层（`frontend/src/__tests__/mobileFormat.test.ts`），
//   由**桌面端套件**（`./scripts/test-frontend.sh`）统一守护 —— "测试在哪、门禁在哪"。
//   （H5 侧的 v8 覆盖统计不到 root 之外的文件，`allowExternal` 实测也不生效 → 留在那边等于没人守。）
//
// 为什么需要「颜色翻译」这一层: 共享核心层（@pa/core/services/*）里的颜色是**桌面端 antd 口径**
// （`green` / `gold` / `red` / `processing`…），而 antd-mobile 的 `Tag` 只认
// `default | primary | success | warning | danger` 或自定义色值 —— 直接把 `gold` 塞进去会**静默失效**，
// 变成灰色 Tag（扫视找异常的能力就此丢失，这正是审批列表最关键的锚点）。
// RN 侧同理：`mobile-rn/src/theme/tagColors.ts` 把这些语义名再映射成具体色值。
export type MobileTagColor = "default" | "primary" | "success" | "warning" | "danger";

/** antd 预设色 → antd-mobile Tag 色（未识别时回落 default，绝不抛错）。 */
export function toTagColor(color?: string | null): MobileTagColor {
  switch (color) {
    case "green":
    case "success":
      return "success";
    case "gold":
    case "orange":
    case "volcano":
    case "warning":
    case "processing":
      return "warning";
    case "red":
    case "error":
      return "danger";
    case "blue":
    case "geekblue":
    case "primary":
    case "cyan":
    case "purple":
      return "primary";
    default:
      return "default";
  }
}

/**
 * 时间格式化：手机列表行宽有限，用「今年省年份」的紧凑格式。
 *
 * 参数:
 *   iso: ISO 时间串（后端返回 `created_at` 等）；非法/空 → "-"。
 *   now: 参考时刻（默认当前；测试注入以去掉墙钟依赖）。
 */
export function formatTime(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.getFullYear() === now.getFullYear() ? stamp : `${date.getFullYear()}-${stamp}`;
}

/**
 * 相对时间（审批列表比绝对时间更有用：审核员关心「压了多久」）。
 * <1min 刚刚 / <60min N 分钟前 / <24h N 小时前 / 其它走 formatTime。
 */
export function relativeTime(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) return formatTime(iso, now); // 时钟漂移：不显示「-3 分钟前」误导人
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return formatTime(iso, now);
}

/** 价格展示（后端是 numeric，经 JSON 可能是字符串）。 */
export function formatPrice(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const num = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(num)) return "-";
  return `¥ ${num}`;
}

/** 超长文本截断（正则、错误原因在列表里的展示用）。 */
export function truncate(text?: string | null, max = 60): string {
  if (!text) return "";
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

/** 秒 → 人类可读（心跳 TTL / 重试间隔）。 */
export function formatSeconds(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时`;
}

