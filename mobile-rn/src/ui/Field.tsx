// 表单字段（Input / TextArea 的最小替代：label + 输入 + 错误文案）。
//
// 手机上的两条必备处理:
//   ① `keyboardType` / `autoCapitalize` / `secureTextEntry` 必须显式给（否则用户名会被首字母大写、
//      密码会明文显示 —— 这类问题在浏览器上被自动填充"掩盖"，在原生端会直接暴露）；
//   ② 校验错误就显示在字段下方（不用弹窗），与 H5 的 `Form.Item rules` 体验一致。
import { StyleSheet, Text, TextInput, View, type TextInputProps, type ViewStyle } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

interface BaseProps {
  label: string;
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
  error?: string | null;
  help?: string;
  editable?: boolean;
  testID?: string;
  style?: ViewStyle;
}

/** 带标签的单行输入（错误文案优先于帮助文案显示，二者不同时出现）。 */
export function LabeledInput({
  label,
  value,
  onChangeText,
  placeholder,
  error,
  help,
  editable = true,
  testID,
  style,
  ...rest
}: BaseProps & Pick<TextInputProps, "secureTextEntry" | "keyboardType" | "autoCapitalize" | "autoComplete" | "maxLength">) {
  return (
    <View style={[styles.field, style]}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        testID={testID}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={colors.textDisabled}
        editable={editable}
        accessibilityLabel={label}
        style={[styles.input, !editable && styles.inputDisabled, error ? styles.inputError : null]}
        {...rest}
      />
      {error ? <Text style={styles.error}>{error}</Text> : help ? <Text style={styles.help}>{help}</Text> : null}
    </View>
  );
}

/** 带标签的多行输入（驳回意见、商品描述用；`maxLength` 与后端字段上限对齐时传入）。 */
export function LabeledTextArea({
  label,
  value,
  onChangeText,
  placeholder,
  error,
  help,
  editable = true,
  rows = 4,
  testID,
  style,
}: BaseProps & { rows?: number }) {
  return (
    <View style={[styles.field, style]}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        testID={testID}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={colors.textDisabled}
        editable={editable}
        multiline
        textAlignVertical="top"
        accessibilityLabel={label}
        style={[
          styles.input,
          styles.textArea,
          { minHeight: 24 * rows },
          !editable && styles.inputDisabled,
          error ? styles.inputError : null,
        ]}
      />
      {error ? <Text style={styles.error}>{error}</Text> : help ? <Text style={styles.help}>{help}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  field: { marginBottom: space.md },
  label: { fontSize: font.sm, color: colors.textSecondary, marginBottom: space.xs },
  input: {
    minHeight: TOUCH_TARGET,
    borderWidth: 1,
    borderColor: colors.borderStrong,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    fontSize: font.md,
    color: colors.text,
    backgroundColor: colors.bgCard,
  },
  textArea: { lineHeight: 20 },
  inputDisabled: { backgroundColor: "#f7f7f7", color: colors.textSecondary },
  inputError: { borderColor: colors.danger },
  error: { fontSize: font.xs, color: colors.danger, marginTop: space.xs },
  help: { fontSize: font.xs, color: colors.textSecondary, marginTop: space.xs },
});
