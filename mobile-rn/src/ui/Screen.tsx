// 页面容器（= H5 的 `.pa-page` / `.pa-page--actions` 两个 class 的等价物）。
//
// 为什么需要它: 手机上有两块"会遮住内容"的东西 —— 底部 TabBar 与固定操作栏。
// 每个页面各自算 padding 迟早会漏，统一在这里按用途给：
//   · 默认（Tab 页）：留出 TabBar 高度 + 底部安全区；
//   · `withActionBar`（详情页）：再留出固定操作栏高度。
// 另外：页面根统一走 `SafeAreaView`（顶部刘海）+ 浅灰背景，避免各页各写一套。
import { ScrollView, StyleSheet, View, type ViewStyle } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, TAB_BAR_HEIGHT } from "./theme";

/** 固定操作栏高度（含内边距），留给 `withActionBar` 页面做底部留白 */
export const ACTION_BAR_HEIGHT = 62;

interface Props {
  children: React.ReactNode;
  /** 是否用 ScrollView 包裹（详情页需要；列表页自己用 FlatList，传 false） */
  scroll?: boolean;
  /** 页面是否有固定底部操作栏（商品详情/审批详情） */
  withActionBar?: boolean;
  style?: ViewStyle;
  testID?: string;
}

export default function Screen({ children, scroll = true, withActionBar = false, style, testID }: Props) {
  const insets = useSafeAreaInsets();
  const paddingBottom = (withActionBar ? ACTION_BAR_HEIGHT + TAB_BAR_HEIGHT : TAB_BAR_HEIGHT) + insets.bottom;

  if (!scroll) {
    return (
      <View style={[styles.page, style]} testID={testID}>
        {children}
      </View>
    );
  }
  return (
    <ScrollView
      style={styles.page}
      contentContainerStyle={[styles.content, { paddingBottom }]}
      keyboardShouldPersistTaps="handled"
      testID={testID}
    >
      {children}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  content: { flexGrow: 1 },
});
