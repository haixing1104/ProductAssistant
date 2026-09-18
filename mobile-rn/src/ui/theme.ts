// RN 主题 token —— **颜色语义与 H5 的 antd-mobile 口径对齐**（这是 ADR 的一部分，不是样式偏好）。
//
// 为什么这份文件重要: 手机上"表格 → 卡片"之后，Tag 的颜色是**扫视找异常的唯一锚点**
//（评估分 ≥90 绿 / ≥80 金 / <80 红；审批待处理金 / 已批准绿 / 已驳回红）。
// 各写各的色值 = 同一个后端状态在两端看起来不一样，审核员在两台设备上会做不同判断。
import type { MobileTagColor } from "@pa/core/services/mobileFormat";

export const colors = {
  primary: "#1677ff",
  success: "#00b578",
  warning: "#ff8f1f",
  danger: "#ff3141",
  text: "#333333",
  textSecondary: "#999999",
  textDisabled: "#bbbbbb",
  border: "#eeeeee",
  borderStrong: "#e5e5e5",
  bg: "#f5f6f8",
  bgCard: "#ffffff",
  bgMuted: "#fafafa",
} as const;

/** 间距（4 的倍数，与 antd-mobile 的视觉节奏一致） */
export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 } as const;

export const radius = { sm: 6, md: 8, lg: 12 } as const;

export const font = { xs: 12, sm: 13, md: 15, lg: 17, xl: 20 } as const;

/** 触控目标下限（iOS HIG 44pt；Android 48dp 由各组件 hitSlop 补足） */
export const TOUCH_TARGET = 44;

/** TabBar 高度（页面底部要留出这么多，避免最后一条内容被压住） */
export const TAB_BAR_HEIGHT = 50;

export interface TagPalette {
  bg: string;
  fg: string;
  border: string;
}

/** 语义色 → RN 色值（与 antd-mobile 的 default/primary/success/warning/danger 视觉一致）。 */
export const TAG_PALETTE: Record<MobileTagColor, TagPalette> = {
  default: { bg: "#f5f5f5", fg: "#666666", border: "#dddddd" },
  primary: { bg: "#e7f1ff", fg: "#1677ff", border: "#a3c9ff" },
  success: { bg: "#e8f8f1", fg: "#00b578", border: "#8ee0bd" },
  warning: { bg: "#fff3e6", fg: "#ff8f1f", border: "#ffd199" },
  danger: { bg: "#ffe9ea", fg: "#ff3141", border: "#ffb3b8" },
};

/** 未知/空颜色一律回落 default（**绝不抛错**：颜色不该让页面崩）。 */
export function tagPalette(color?: string | null): TagPalette {
  const key = (color ?? "default") as MobileTagColor;
  return TAG_PALETTE[key] ?? TAG_PALETTE.default;
}

/** 实心/描边两种填充的派生色（Tag 用）。 */
export function tagFilled(color?: string | null): TagPalette {
  const palette = tagPalette(color);
  return { bg: palette.fg, fg: "#ffffff", border: palette.fg };
}
