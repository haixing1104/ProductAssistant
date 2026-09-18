// Chips（= antd-mobile `Selector` 的单选替身）：状态筛选、库存选项都用它。
//
// 为什么不用系统 Picker: 选项只有 3~5 个且需要"当前选中"一直可见（手机上的下拉会把选项藏起来，
// 运营在列表页反复切状态时多一次点击就是多一次出错机会）。
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

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
      contentContainerStyle={styles.scrollContent}
      testID={testID}
    >
      {body}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: space.md, paddingVertical: space.sm },
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
