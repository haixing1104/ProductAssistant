// Card（= antd-mobile `Card` 的最小可用替代：标题 + 右上角 extra + 内容区，可整卡点按）。
//
// 移动端口径: 卡片是"一次决策所需的字段集合"（替代桌面表格的一行），因此
//   · 标题行 = 商品名/SKU（扫视主锚点）；extra = 状态 Tag；
//   · 整卡可点（进入详情），卡内按钮必须 `stopPropagation`（否则误触会连带进详情）。
import { Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

interface Props {
  title?: React.ReactNode;
  extra?: React.ReactNode;
  children?: React.ReactNode;
  onPress?: () => void;
  style?: ViewStyle;
  testID?: string;
}

export default function Card({ title, extra, children, onPress, style, testID }: Props) {
  const content = (
    <>
      {title !== undefined || extra !== undefined ? (
        <View style={styles.header}>
          <View style={styles.titleWrap}>
            {typeof title === "string" ? (
              <Text style={styles.title} numberOfLines={2} allowFontScaling maxFontSizeMultiplier={1.3}>
                {title}
              </Text>
            ) : (
              title
            )}
          </View>
          {extra}
        </View>
      ) : null}
      <View>{children}</View>
    </>
  );

  if (!onPress) {
    return (
      <View style={[styles.card, style]} testID={testID}>
        {content}
      </View>
    );
  }
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.card, { opacity: pressed ? 0.85 : 1 }, style]}
    >
      {content}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.bgCard,
    borderRadius: radius.md,
    paddingVertical: space.md,
    minHeight: TOUCH_TARGET,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: space.sm,
    paddingHorizontal: space.md,
    marginBottom: space.sm,
  },
  titleWrap: { flex: 1 },
  title: { fontSize: font.md, fontWeight: "600", color: colors.text },
});
