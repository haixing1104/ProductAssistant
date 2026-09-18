// Button（= antd-mobile `Button` 的最小可用替代：primary/danger/default × solid/outline/none × mini/small/large）。
//
// 移动端两条硬要求（照抄桌面端或 H5 的 CSS 都会漏）:
//   ① 触控目标 ≥44×44：`hitSlop` 补足视觉上的小按钮（mini 的实际高度只有 ~24）；
//   ② 按下要有反馈：`Pressable` 的 pressed 态改背景/透明度 —— 手机上"点上去没反应"会被误判为卡死。
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

export type ButtonVariant = "primary" | "danger" | "default";
export type ButtonFill = "solid" | "outline" | "none";
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

const SOLID: Record<ButtonVariant, string> = {
  primary: colors.primary,
  danger: colors.danger,
  default: "#f5f5f5",
};

const OUTLINE_TEXT: Record<ButtonVariant, string> = {
  primary: colors.primary,
  danger: colors.danger,
  default: "#666666",
};

const SOLID_TEXT: Record<ButtonVariant, string> = {
  primary: "#ffffff",
  danger: "#ffffff",
  default: colors.text,
};

const HEIGHT: Record<ButtonSize, number> = { mini: 26, small: 32, large: 46 };
const PAD: Record<ButtonSize, number> = { mini: 10, small: 12, large: 20 };
const TEXT_SIZE: Record<ButtonSize, number> = { mini: font.xs, small: font.sm, large: font.md };

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
