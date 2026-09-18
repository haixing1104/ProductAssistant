// 列表原语（= antd-mobile `List` / `List.Item` / `ErrorBlock` 的最小可用替代）。
//
// 三件事必须做到（H5 的教训）:
//   ① 查询失败与"没有数据"必须**长得不一样**：把接口失败画成空列表，用户会以为"这里本来就没内容"；
//   ② 长文本（AI 正文、驳回意见、失败原因）保留换行、不撑破容器；
//   ③ 行的"标签 + 右侧值"结构固定，避免各页面各写一套导致扫视节奏不一致。
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";

import { colors, font, radius, space } from "./theme";

/**
 * 把裸字符串/数字包进 `<Text>`。
 *
 * 为什么必须做: RN 里字符串**只能**出现在 `<Text>` 下，否则运行时报
 * `Invariant Violation: Text strings must be rendered within a <Text> component`——
 * 而 Web 上（H5/桌面）这样写完全正常，属于"照抄 Web 写法必踩"的一类。
 * 放在组件内部统一兜住，比要求每个调用点都记得包 `<Text>` 可靠。
 */
function inline(node: React.ReactNode): React.ReactNode {
  if (typeof node === "string" || typeof node === "number") {
    return (
      <Text style={styles.inlineText} allowFontScaling maxFontSizeMultiplier={1.4}>
        {node}
      </Text>
    );
  }
  return node;
}

/** 分组容器（带 header 与卡片外框）。 */
export function ListSection({
  header,
  children,
  style,
  testID,
}: {
  header?: React.ReactNode;
  children: React.ReactNode;
  style?: ViewStyle;
  testID?: string;
}) {
  return (
    <View style={[styles.section, style]} testID={testID}>
      {header ? (
        <Text style={styles.sectionHeader} allowFontScaling maxFontSizeMultiplier={1.3}>
          {header}
        </Text>
      ) : null}
      <View style={styles.sectionBody}>{children}</View>
    </View>
  );
}

/** 一行（label + 右侧 extra；description 走第二行小字）。 */
export function ListRow({
  label,
  extra,
  description,
  children,
  onPress,
  testID,
}: {
  label?: React.ReactNode;
  extra?: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  onPress?: () => void;
  testID?: string;
}) {
  const body = (
    <>
      {label !== undefined || extra !== undefined ? (
        <View style={styles.rowLine}>
          {typeof label === "string" ? <Text style={styles.rowLabel}>{label}</Text> : label}
          <View style={styles.rowExtra}>
            {typeof extra === "string" ? <Text style={styles.rowExtraText}>{extra}</Text> : extra}
          </View>
        </View>
      ) : null}
      {inline(children)}
      {description !== undefined ? <View style={styles.rowDescription}>{inline(description)}</View> : null}
    </>
  );
  if (!onPress) {
    return (
      <View style={styles.row} testID={testID}>
        {body}
      </View>
    );
  }
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.row, { opacity: pressed ? 0.85 : 1 }]}
    >
      {body}
    </Pressable>
  );
}

/** 空态 / 失败态（`tone="error"` 时给红色标题，避免与"真的没数据"混淆）。 */
export function EmptyState({
  title,
  description,
  tone = "muted",
  action,
  testID,
}: {
  title: string;
  description?: string;
  tone?: "muted" | "error";
  action?: React.ReactNode;
  testID?: string;
}) {
  return (
    <View style={styles.empty} testID={testID}>
      <Text
        style={[styles.emptyTitle, tone === "error" && { color: colors.danger }]}
        allowFontScaling
        maxFontSizeMultiplier={1.3}
      >
        {title}
      </Text>
      {description ? <Text style={styles.emptyDescription}>{description}</Text> : null}
      {action ? <View style={styles.emptyAction}>{action}</View> : null}
    </View>
  );
}

/** 加载中（统一口径：不要各页面自己写 ActivityIndicator 的位置与大小）。 */
export function Loading({ text = "加载中…", testID }: { text?: string; testID?: string }) {
  return (
    <View style={styles.empty} testID={testID}>
      <ActivityIndicator color={colors.primary} />
      <Text style={styles.emptyDescription}>{text}</Text>
    </View>
  );
}

/** 长文本（保留换行）—— 与 H5 的 `.pa-pre-wrap` 同义。 */
export function PreWrapText({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: object;
}) {
  return (
    <Text style={[styles.preWrap, style]} allowFontScaling maxFontSizeMultiplier={1.4}>
      {children}
    </Text>
  );
}

const styles = StyleSheet.create({
  section: { marginTop: space.sm, marginHorizontal: space.md },
  sectionHeader: {
    fontSize: font.xs,
    color: colors.textSecondary,
    marginBottom: space.xs,
    marginLeft: space.xs,
  },
  sectionBody: {
    backgroundColor: colors.bgCard,
    borderRadius: radius.md,
    overflow: "hidden",
  },
  row: {
    paddingHorizontal: space.md,
    paddingVertical: space.sm + 2,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  rowLine: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: space.sm },
  rowLabel: { fontSize: font.md, color: colors.textSecondary },
  rowExtra: { flexShrink: 1, alignItems: "flex-end" },
  rowExtraText: { fontSize: font.md, color: colors.text, textAlign: "right" },
  rowDescription: { marginTop: space.xs },
  empty: { alignItems: "center", justifyContent: "center", padding: space.xl, gap: space.sm },
  emptyTitle: { fontSize: font.md, color: colors.textSecondary, textAlign: "center" },
  emptyDescription: { fontSize: font.sm, color: colors.textSecondary, textAlign: "center", lineHeight: 20 },
  emptyAction: { marginTop: space.sm },
  preWrap: { fontSize: font.md, lineHeight: 22, color: colors.text },
  inlineText: { fontSize: font.md, color: colors.text, lineHeight: 21 },
});
