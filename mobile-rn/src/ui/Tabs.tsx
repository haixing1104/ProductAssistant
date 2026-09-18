// Tabs（= antd-mobile `Tabs` 的最小替代：顶部等宽标签 + 下划线）。
//
// 口径: 审批中心用它切「待我处理（N）/ 已批准 / 已驳回」—— 计数直接写在标题里，
// 因为审核员最关心的是"还有多少条压着"，不该再点一次才能看到。
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, font, space, TOUCH_TARGET } from "./theme";

/** 一个标签项（`title` 可直接带计数，如「待我处理（3）」）。 */
export interface TabItem {
  key: string;
  title: string;
}

interface Props {
  items: TabItem[];
  activeKey: string;
  onChange: (key: string) => void;
  testID?: string;
}

/** 等宽标签切换（受控：`activeKey` + `onChange`，与 antd-mobile Tabs 同用法）。 */
export default function Tabs({ items, activeKey, onChange, testID }: Props) {
  return (
    <View style={styles.bar} testID={testID}>
      {items.map((item) => {
        const active = item.key === activeKey;
        return (
          <Pressable
            key={item.key}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            onPress={() => onChange(item.key)}
            style={styles.tab}
          >
            <Text style={[styles.text, active && styles.textActive]} numberOfLines={1}>
              {item.title}
            </Text>
            <View style={[styles.underline, active && styles.underlineActive]} />
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: "row",
    backgroundColor: colors.bgCard,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  tab: { flex: 1, height: TOUCH_TARGET, alignItems: "center", justifyContent: "center" },
  text: { fontSize: font.md, color: colors.text },
  textActive: { color: colors.primary, fontWeight: "600" },
  underline: { position: "absolute", bottom: 0, height: 2, width: "40%", backgroundColor: "transparent" },
  underlineActive: { backgroundColor: colors.primary },
});
