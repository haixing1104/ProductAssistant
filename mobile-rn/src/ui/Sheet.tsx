// Sheet / ActionSheet（= antd-mobile `Popup` + `ActionSheet` 的最小替代）。
//
// 为什么必须有: 手机上"低频危险操作"（彻底删除）不能放在列表里 —— 误触代价极高。
// 统一收进底部动作面板，并且 danger 项用红色 + 二次确认（页面负责二次确认）。
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

/** 通用底部弹层（表单、审批意见、二次确认都用它）。 */
export function Sheet({
  visible,
  onClose,
  children,
  testID,
}: {
  visible: boolean;
  onClose: () => void;
  children: React.ReactNode;
  testID?: string;
}) {
  const insets = useSafeAreaInsets();
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View style={styles.maskWrap}>
        {/* 点遮罩关闭：手机上唯一可靠的"取消"手势（Android 返回键由 onRequestClose 兜住） */}
        <Pressable style={styles.mask} onPress={onClose} accessibilityLabel="关闭" />
        <View style={[styles.sheet, { paddingBottom: insets.bottom + space.sm }]} testID={testID}>
          {children}
        </View>
      </View>
    </Modal>
  );
}

/** 动作面板里的一项（`danger` 用红色文字：删除这类不可逆操作必须视觉区分）。 */
export interface SheetAction {
  key: string;
  text: string;
  danger?: boolean;
  disabled?: boolean;
}

/** 动作面板（只有一排动作 + 取消）。 */
export function ActionSheet({
  visible,
  actions,
  onAction,
  cancelText = "取消",
  onClose,
  testID,
}: {
  visible: boolean;
  actions: SheetAction[];
  onAction: (action: SheetAction) => void;
  cancelText?: string;
  onClose: () => void;
  testID?: string;
}) {
  return (
    <Sheet visible={visible} onClose={onClose} testID={testID}>
      <ScrollView bounces={false}>
        {actions.map((action) => (
          <Pressable
            key={action.key}
            accessibilityRole="button"
            accessibilityState={{ disabled: Boolean(action.disabled) }}
            disabled={Boolean(action.disabled)}
            onPress={() => onAction(action)}
            style={({ pressed }) => [styles.action, { opacity: pressed || action.disabled ? 0.5 : 1 }]}
          >
            <Text style={[styles.actionText, action.danger && { color: colors.danger }]}>{action.text}</Text>
          </Pressable>
        ))}
      </ScrollView>
      <Pressable
        accessibilityRole="button"
        onPress={onClose}
        style={({ pressed }) => [styles.cancel, { opacity: pressed ? 0.7 : 1 }]}
      >
        <Text style={styles.cancelText}>{cancelText}</Text>
      </Pressable>
    </Sheet>
  );
}

const styles = StyleSheet.create({
  maskWrap: { flex: 1, justifyContent: "flex-end" },
  mask: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, backgroundColor: "rgba(0,0,0,0.45)" },
  sheet: {
    backgroundColor: colors.bg,
    borderTopLeftRadius: radius.lg,
    borderTopRightRadius: radius.lg,
    paddingTop: space.sm,
  },
  action: {
    minHeight: TOUCH_TARGET + 6,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.bgCard,
    marginTop: StyleSheet.hairlineWidth,
  },
  actionText: { fontSize: font.md, color: colors.text },
  cancel: {
    minHeight: TOUCH_TARGET + 6,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.bgCard,
    marginTop: space.sm,
  },
  cancelText: { fontSize: font.md, color: colors.textSecondary, fontWeight: "600" },
});
