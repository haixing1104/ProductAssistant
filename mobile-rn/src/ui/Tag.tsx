// Tag（= antd-mobile `Tag` 的最小可用替代）。
//
// 关键约束（与 H5 同口径）: 颜色必须来自 `theme.tagPalette`（语义色），而不是各处手写色值；
// `fill="solid"` 用于需要"一眼看到"的场合（评估分），`outline` 用于次要信息（SKU/库存）。
// 无障碍: 颜色**不是唯一信息载体** —— 文案本身必须能读懂（色盲用户同样要能扫视）。
import { StyleSheet, Text, View } from "react-native";

import { font, radius, space, tagFilled, tagPalette } from "./theme";

interface Props {
  children: React.ReactNode;
  /** 语义色（来自共享层的 `*Color()` / `toTagColor()`），未知值回落 default */
  color?: string | null;
  fill?: "outline" | "solid";
  style?: object;
}

/** Tag（颜色必须走 `tagPalette`/`tagFilled`；`solid` 用于必须一眼看到的场合）。 */
export default function Tag({ children, color, fill = "outline", style }: Props) {
  const palette = fill === "solid" ? tagFilled(color) : tagPalette(color);
  return (
    <View
      style={[styles.base, { backgroundColor: palette.bg, borderColor: palette.border }, style]}
      accessibilityRole="text"
    >
      <Text style={[styles.text, { color: palette.fg }]} allowFontScaling maxFontSizeMultiplier={1.4}>
        {children}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  base: {
    borderWidth: 1,
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    // 竖向内边距刻意小：手机列表里 Tag 是"扫视锚点"，不该抢走行高
    paddingVertical: 2,
    alignSelf: "flex-start",
  },
  text: { fontSize: font.xs, lineHeight: 16 },
});
