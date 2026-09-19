// Chips（= antd-mobile `Selector` 的单选替身）：状态筛选、库存选项都用它。
//
// 为什么不用系统 Picker: 选项只有 3~5 个且需要"当前选中"一直可见（手机上的下拉会把选项藏起来，
// 运营在列表页反复切状态时多一次点击就是多一次出错机会）。
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

/** 一个选项（`value` 参与请求参数，`label` 只用于展示）。 */
export interface ChipOption {
  value: string;
  label: string;
}

interface Props {
  options: ChipOption[];
  value: string;
  onChange: (value: string) => void;
  /** 是否允许横向滚动（选项多时） */
  scrollable?: boolean;
  testID?: string;
}

/** 单选 Chips（受控；`scrollable` 用于选项较多的筛选行）。 */
export default function Chips({ options, value, onChange, scrollable = false, testID }: Props) {
  const body = (
    <View style={styles.row}>
      {options.map((option) => {
        const active = option.value === value;
        return (
          <Pressable
            key={option.value}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            onPress={() => onChange(option.value)}
            hitSlop={{ top: 6, bottom: 6, left: 2, right: 2 }}
            style={({ pressed }) => [
              styles.chip,
              active && styles.chipActive,
              { opacity: pressed ? 0.7 : 1 },
            ]}
          >
            <Text style={[styles.chipText, active && styles.chipTextActive]}>{option.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );

  if (!scrollable) {
    return (
      <View style={styles.wrap} testID={testID}>
        {body}
      </View>
    );
  }
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      /**
       * ⚠️ **必须显式压掉 RN ScrollView 的 `baseHorizontal`**（RN 0.86 `ScrollView.js:1887`：
       * `{ flexGrow: 1, flexShrink: 1, flexDirection: "row" }`）。
       *
       * 不写 style 时 `flexGrow: 1` 作用在**父容器的竖直主轴**上 → 这条横向筛选行会纵向长大去吃空白，
       * 把下面的列表推到屏幕中段；列表自己也带 `flexGrow: 1`，两者平分空白，于是「只剩 1 条数据时
       * 看起来垂直居中、上下各留一块空白」（2026-09 真机截图定位到就是它）。
       * 横向 chips 行的高度只该由内容决定 —— 所以 flexGrow/flexShrink 都归零。
       */
      style={styles.scroll}
      contentContainerStyle={styles.scrollContent}
      testID={testID}
    >
      {body}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: space.md, paddingVertical: space.sm },
  /** 横向筛选行：高度由内容决定（见上面 ⚠️；不能让它纵向 flexGrow）。 */
  scroll: { flexGrow: 0, flexShrink: 0 },
  scrollContent: { paddingHorizontal: space.md, paddingVertical: space.sm },
  row: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  chip: {
    minHeight: 30,
    minWidth: TOUCH_TARGET - 14,
    paddingHorizontal: space.md,
    justifyContent: "center",
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.borderStrong,
    backgroundColor: colors.bgCard,
  },
  chipActive: { borderColor: colors.primary, backgroundColor: "#e7f1ff" },
  chipText: { fontSize: font.sm, color: colors.text },
  chipTextActive: { color: colors.primary, fontWeight: "600" },
});
