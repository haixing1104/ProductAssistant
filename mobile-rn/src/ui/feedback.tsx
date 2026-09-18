// 命令式反馈（Toast / Dialog）—— **调用习惯与 H5 完全一致**：页面里直接 `Toast.show(...)` / `Dialog.confirm(...)`。
//
// 为什么自研而不引库:
//   RN 没有浏览器 Toast；Android 有原生 Toast 但 **iOS 没有**（直接用会让两端表现不一致）。
//   自研 host 保证双端一致，而且下面的 emitter 是**纯逻辑**（不依赖渲染），可以被单测直接断言 ——
//   这正是 H5 里 `Toast.show` / `Dialog` 曾经"静默不渲染"那次事故该有的兜底方式。
import { useEffect, useState } from "react";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, font, radius, space, TOUCH_TARGET } from "./theme";

// ============================== Toast ==============================

/** Toast 图标语义（`null` = 纯文字提示）。 */
export type ToastIcon = "success" | "fail" | null;

/** Toast 参数（`content` 必填，其余可选）。 */
export interface ToastOptions {
  content: string;
  icon?: ToastIcon;
  /** 毫秒；默认 2000 */
  duration?: number;
}

type ToastListener = (options: Required<ToastOptions>) => void;

/** 订阅者集合（Host 挂载时注册；**纯逻辑、不依赖渲染**，因此可被单测直接断言）。 */
const toastListeners = new Set<ToastListener>();

/** 命令式 Toast API（`show` 无 Host 时静默丢弃：提示类不阻塞主流程）。 */
export const Toast = {
  /** `Toast.show("已保存")` 或 `Toast.show({ content, icon: "fail" })`（两种都支持，与 H5 用法一致）。 */
  show(options: ToastOptions | string): void {
    const value: ToastOptions = typeof options === "string" ? { content: options } : options;
    const payload: Required<ToastOptions> = {
      content: value.content,
      icon: value.icon ?? null,
      duration: value.duration ?? 2000,
    };
    // 复制一份再遍历：host 可能在回调里卸载（页面切换）
    [...toastListeners].forEach((listener) => listener(payload));
  },
};

// ============================== Dialog ==============================

/** 确认框参数。 */
export interface DialogOptions {
  title?: string;
  content?: string;
  confirmText?: string;
  /** 传 null 表示只有一个按钮（alert 语义） */
  cancelText?: string | null;
  danger?: boolean;
  onConfirm?: () => void | Promise<void>;
}

type DialogListener = (options: DialogOptions, settle: (ok: boolean) => void) => void;

/** 订阅者集合（与 toastListeners 同机制）。 */
const dialogListeners = new Set<DialogListener>();

/**
 * 发起一次确认框并等用户选择。
 *
 * 返回:
 *   用户点确认 → true；点取消 → false。
 * 注意:
 *   **没有 Host 时 resolve(false)** —— 明确失败而不是静默挂起，
 *   否则 `await Dialog.confirm(...)` 会永远不返回（页面看着像卡死）。
 */
function openDialog(options: DialogOptions): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const listeners = [...dialogListeners];
    if (listeners.length === 0) {
      // 没有 host（组件树还没挂载）：**明确失败**，不要静默什么都不做
      resolve(false);
      return;
    }
    listeners.forEach((listener) => listener(options, resolve));
  });
}

/** 命令式 Dialog API（`alert` = 单按钮，`confirm` = 双按钮）。 */
export const Dialog = {
  alert(options: DialogOptions): Promise<boolean> {
    return openDialog({ ...options, cancelText: null });
  },
  confirm(options: DialogOptions): Promise<boolean> {
    return openDialog(options);
  },
};

/** 测试用：清空订阅（避免用例之间互相串提示）。 */
export function resetFeedbackListeners(): void {
  toastListeners.clear();
  dialogListeners.clear();
}

// ============================== Host ==============================

/** 图标字形（RN 无图标字体依赖：用 Unicode 字形保证双端一致且零依赖）。 */
const ICON_GLYPH: Record<Exclude<ToastIcon, null>, string> = { success: "✓", fail: "✕" };

interface DialogState {
  options: DialogOptions;
  settle: (ok: boolean) => void;
}

/**
 * 反馈宿主：**必须在 App 根挂载一次**（否则 Toast.show / Dialog 是空操作）。
 * 它就是与 H5 的 `Toast`/`Dialog` 命令式 API 对应的那一层"渲染实现"。
 */
export function FeedbackHost() {
  const [toast, setToast] = useState<{ content: string; icon: ToastIcon } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);

  useEffect(() => {
    const onToast: ToastListener = ({ content, icon, duration }) => {
      setToast({ content, icon });
      // 后一条提示覆盖前一条：手机上排队弹过期提示比"覆盖"更糟
      setTimeout(() => setToast((current) => (current?.content === content ? null : current)), duration);
    };
    const onDialog: DialogListener = (options, settle) => setDialog({ options, settle });
    toastListeners.add(onToast);
    dialogListeners.add(onDialog);
    return () => {
      toastListeners.delete(onToast);
      dialogListeners.delete(onDialog);
    };
  }, []);

  const settle = (ok: boolean) => {
    const current = dialog;
    if (!current) return;
    setDialog(null);
    current.settle(ok);
    // onConfirm 里常有异步写操作（审批/删除）：失败由业务侧自己 Toast，这里不吞真实错误
    if (ok && current.options.onConfirm) {
      void Promise.resolve()
        .then(() => current.options.onConfirm?.())
        .catch(() => undefined);
    }
  };

  return (
    <>
      {toast ? (
        <View style={styles.toastLayer} pointerEvents="none" testID="pa-toast">
          <View style={styles.toast}>
            {toast.icon ? <Text style={styles.toastIcon}>{ICON_GLYPH[toast.icon]}</Text> : null}
            <Text style={styles.toastText} allowFontScaling maxFontSizeMultiplier={1.4}>
              {toast.content}
            </Text>
          </View>
        </View>
      ) : null}

      <Modal visible={dialog !== null} transparent animationType="fade" onRequestClose={() => settle(false)}>
        <View style={styles.dialogMask} testID="pa-dialog">
          <View style={styles.dialogCard}>
            {dialog?.options.title ? <Text style={styles.dialogTitle}>{dialog.options.title}</Text> : null}
            {dialog?.options.content ? (
              <Text style={styles.dialogContent} allowFontScaling maxFontSizeMultiplier={1.4}>
                {dialog.options.content}
              </Text>
            ) : null}
            <View style={styles.dialogActions}>
              {dialog?.options.cancelText === null ? null : (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={dialog?.options.cancelText ?? "取消"}
                  onPress={() => settle(false)}
                  style={({ pressed }) => [styles.dialogButton, { opacity: pressed ? 0.6 : 1 }]}
                >
                  <Text style={styles.dialogButtonText}>{dialog?.options.cancelText ?? "取消"}</Text>
                </Pressable>
              )}
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={dialog?.options.confirmText ?? "确定"}
                onPress={() => settle(true)}
                style={({ pressed }) => [styles.dialogButton, { opacity: pressed ? 0.6 : 1 }]}
              >
                <Text
                  style={[
                    styles.dialogButtonText,
                    {
                      color: dialog?.options.danger ? colors.danger : colors.primary,
                      fontWeight: "600",
                    },
                  ]}
                >
                  {dialog?.options.confirmText ?? "确定"}
                </Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </>
  );
}


const styles = StyleSheet.create({
  toastLayer: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
  },
  toast: {
    maxWidth: "80%",
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    backgroundColor: "rgba(0,0,0,0.8)",
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  toastIcon: { color: "#ffffff", fontSize: font.md },
  toastText: { color: "#ffffff", fontSize: font.md, textAlign: "center", flexShrink: 1 },
  dialogMask: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", alignItems: "center", justifyContent: "center" },
  dialogCard: { width: "84%", backgroundColor: colors.bgCard, borderRadius: radius.lg, paddingTop: space.lg },
  dialogTitle: {
    fontSize: font.lg,
    fontWeight: "600",
    color: colors.text,
    textAlign: "center",
    paddingHorizontal: space.lg,
  },
  dialogContent: {
    fontSize: font.md,
    color: colors.text,
    lineHeight: 22,
    paddingHorizontal: space.lg,
    marginTop: space.sm,
  },
  dialogActions: {
    flexDirection: "row",
    marginTop: space.lg,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
  },
  dialogButton: { flex: 1, minHeight: TOUCH_TARGET, alignItems: "center", justifyContent: "center" },
  dialogButtonText: { fontSize: font.md, color: colors.textSecondary },
});

