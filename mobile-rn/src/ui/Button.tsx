// Button（= antd-mobile `Button` 的最小可用替代：primary/danger/default × solid/outline/none × mini/small/large）。
//
// 移动端两条硬要求（照抄桌面端或 H5 的 CSS 都会漏）:
//   ① 触控目标 ≥44×44：`hitSlop` 补足视觉上的小按钮（mini 的实际高度只有 ~24）；
//   ② 按下要有反馈：`Pressable` 的 pressed 态改背景/透明度 —— 手机上"点上去没反应"会被误判为卡死。
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

/** 按钮语义（primary 主操作 / danger 危险操作 / default 次要）。 */
export type ButtonVariant = "primary" | "danger" | "default";
/** 填充方式（solid 实心 / outline 描边 / none 纯文字）。 */
export type ButtonFill = "solid" | "outline" | "none";
/** 尺寸（mini 用于卡内操作；large 用于底部固定操作栏主按钮）。 */
export type ButtonSize = "mini" | "small" | "large";

interface Props {
  children: React.ReactNode;
  onPress?: () => void;
  variant?: ButtonVariant;
  fill?: ButtonFill;
  size?: ButtonSize;
  loading?: boolean;
  disabled?: boolean;
  block?: boolean;
  style?: ViewStyle;
  testID?: string;
  accessibilityLabel?: string;
}

/** 各语义的实心底色（与 antd-mobile Button 视觉一致）。 */
const SOLID: Record<ButtonVariant, string> = {
  primary: colors.primary,
  danger: colors.danger,
  default: "#f5f5f5",
};

/** 描边/纯文字模式下的字色（同时用作描边色，保证文字与边框同色）。 */
const OUTLINE_TEXT: Record<ButtonVariant, string> = {
  primary: colors.primary,
  danger: colors.danger,
  default: "#666666",
};

/** 实心模式下的字色（primary/danger 用白字，default 用深色字）。 */
const SOLID_TEXT: Record<ButtonVariant, string> = {
  primary: "#ffffff",
  danger: "#ffffff",
  default: colors.text,
};

/** 尺寸 → 视觉高度（mini 只有 ~26：可点区域靠 `hitSlop` 补到 44，见下方渲染）。 */
const HEIGHT: Record<ButtonSize, number> = { mini: 26, small: 32, large: 46 };
/** 尺寸 → 左右内边距。 */
const PAD: Record<ButtonSize, number> = { mini: 10, small: 12, large: 20 };
/** 尺寸 → 字号（统一从 theme 取，不允许各页手写）。 */
const TEXT_SIZE: Record<ButtonSize, number> = { mini: font.xs, small: font.sm, large: font.md };

/**
 * 按钮（自研薄 UI，替代 antd-mobile Button）。
 *
 * 两条移动端硬要求（照抄桌面 CSS 会漏）:
 *   ① 触控目标 ≥44：用 `hitSlop` 补足小尺寸按钮；
 *   ② 按下必须有反馈：pressed 态改透明度 —— 手机上「点上去没反应」会被当成卡死。
 */
export default function Button({
  children,
  onPress,
  variant = "default",
  fill = "solid",
  size = "small",
  loading = false,
  disabled = false,
  block = false,
  style,
  testID,
  accessibilityLabel,
}: Props) {
  const inactive = disabled || loading;
  const backgroundColor =
    fill === "solid" ? (inactive ? "#f0f0f0" : SOLID[variant]) : "transparent";
  const textColor =
    fill === "solid" ? (inactive ? colors.textDisabled : SOLID_TEXT[variant]) : OUTLINE_TEXT[variant];
  const borderColor = fill === "outline" ? (inactive ? colors.border : OUTLINE_TEXT[variant]) : "transparent";

  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled: inactive, busy: loading }}
      disabled={inactive}
      onPress={onPress}
      // mini 视觉高度 ~26：靠 hitSlop 把可点区域撑到 44（差值一半分到上下）
      hitSlop={{ top: 8, bottom: 8, left: 4, right: 4 }}
      style={({ pressed }) => [
        styles.base,
        {
          height: HEIGHT[size],
          paddingHorizontal: PAD[size],
          backgroundColor,
          borderColor,
          borderWidth: fill === "outline" ? 1 : 0,
          opacity: pressed && !inactive ? 0.7 : 1,
          alignSelf: block ? "stretch" : "auto",
        },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator size="small" color={textColor} />
      ) : (
        <View style={styles.content}>
          <Text
            style={[styles.text, { fontSize: TEXT_SIZE[size], color: textColor }]}
            allowFontScaling
            maxFontSizeMultiplier={1.4}
            numberOfLines={1}
          >
            {children}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minWidth: TOUCH_TARGET,
    borderRadius: radius.md,
    alignItems: "center",
    justifyContent: "center",
  },
  content: { flexDirection: "row", alignItems: "center", gap: space.xs },
  text: { fontWeight: "500" },
});
