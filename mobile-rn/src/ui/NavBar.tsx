// NavBar（页面标题条）+ NoticeBar（一行提示）—— 两个"每页都要用"的窄组件，合并成一个文件。
//
// NavBar: 详情页左返回、右状态 Tag；列表页（Tab 页）不显示返回（back=null）。
// NoticeBar: H5 用 antd-mobile 的四种 color，这里映射成同一套语义色（alert=橙 / error=红 / info=蓝）。
import { Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, font, space, TOUCH_TARGET } from "./theme";

/** 页面标题条（左侧返回 / 中间标题 / 右侧操作区）；Tab 顶级页传 `back = null` 不显示返回。 */
export function NavBar({
  title,
  back,
  backText = "返回",
  right,
  style,
  testID,
}: {
  title: React.ReactNode;
  /** 传 null（或不传）表示不显示返回 —— Tab 页的顶级页面没有"上一页" */
  back?: (() => void) | null;
  backText?: string;
  right?: React.ReactNode;
  style?: ViewStyle;
  testID?: string;
}) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.navBar, { paddingTop: insets.top }, style]} testID={testID}>
      <View style={styles.navBarRow}>
        <View style={styles.navLeft}>
          {back ? (
            <Pressable
              onPress={back}
              accessibilityRole="button"
              accessibilityLabel={backText}
              // 返回键是高频误触区：给足 hitSlop，但视觉上仍是一行小字
              hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
              style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}
            >
              <Text style={styles.navAction}>{`‹ ${backText}`}</Text>
            </Pressable>
          ) : null}
        </View>
        <View style={styles.navTitleWrap}>
          {typeof title === "string" ? (
            <Text style={styles.navTitle} numberOfLines={1} allowFontScaling maxFontSizeMultiplier={1.3}>
              {title}
            </Text>
          ) : (
            title
          )}
        </View>
        <View style={styles.navRight}>{right}</View>
      </View>
    </View>
  );
}

/** 提示色调映射（与 antd-mobile NoticeBar 的 default/info/alert/error 同义：底部提示要一眼分辨严重性）。 */
const NOTICE_TONES = {
  default: { bg: "#f5f5f5", fg: colors.text },
  info: { bg: "#e7f1ff", fg: "#1677ff" },
  alert: { bg: "#fff3e6", fg: "#d46b08" },
  error: { bg: "#ffe9ea", fg: colors.danger },
} as const;

/** 一行提示条（替代 H5 的 `NoticeBar`；色调语义见 `NOTICE_TONES`）。 */
export function NoticeBar({
  content,
  color = "default",
  testID,
}: {
  content: React.ReactNode;
  color?: keyof typeof NOTICE_TONES;
  testID?: string;
}) {
  const tone = NOTICE_TONES[color];
  return (
    <View style={[styles.notice, { backgroundColor: tone.bg }]} testID={testID}>
      {/* 长文案**换行显示**而不是收起：审核场景里"失败原因"必须一眼读全（手机上 hover/tooltip 不存在） */}
      <Text style={[styles.noticeText, { color: tone.fg }]} allowFontScaling maxFontSizeMultiplier={1.4}>
        {content}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  navBar: {
    backgroundColor: colors.bgCard,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  navBarRow: {
    height: TOUCH_TARGET,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: space.sm,
  },
  navLeft: { minWidth: 56, alignItems: "flex-start" },
  navAction: { fontSize: font.md, color: colors.primary },
  navTitleWrap: { flex: 1, alignItems: "center" },
  navTitle: { fontSize: font.lg, fontWeight: "600", color: colors.text },
  navRight: { minWidth: 56, alignItems: "flex-end" },
  notice: { borderRadius: 6, paddingHorizontal: space.md, paddingVertical: space.sm, marginTop: space.sm },
  noticeText: { fontSize: font.sm, lineHeight: 20 },
});
