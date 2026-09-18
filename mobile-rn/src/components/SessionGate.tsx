// 全局会话过期提示（RN 版）—— 与 H5 的 `SessionGate` 语义一致，只换成自研 Dialog。
//
// 为什么不是"静默硬跳登录页": 用户可能正在看一段 AI 生成结果或填了一半的理由，
// 直接跳走会让人以为是 App 崩了。这里明确告知原因，并说明"登录后会回到原页面"。
//
// 平台差异: H5 监听的是 `window` 事件；RN 从平台端口的 emitter 订阅（同为 AUTH_EXPIRED_EVENT 语义）。
import { useEffect } from "react";

import { Dialog } from "../ui/feedback";
import { getPlatform } from "@pa/core/services/platform";

interface Props {
  /** 用户点「重新登录」后由宿主（App）负责跳转（RN 不能像浏览器那样直接 location.assign） */
  onRelogin: () => void;
}

/** 会话过期弹窗（RN 版）：从平台端口订阅过期事件，「重新登录」由宿主负责跳转。 */
export default function SessionGate({ onRelogin }: Props) {
  useEffect(() => {
    return getPlatform().onAuthExpired(() => {
      void Dialog.alert({
        title: "登录状态已过期",
        content:
          "您的登录状态已过期或已被吊销。为保障账号安全，请重新登录后继续操作。\n正在生成的内容不会被丢弃，登录后会回到原页面。",
        confirmText: "重新登录",
        onConfirm: onRelogin,
      });
    });
    // onRelogin 由宿主每次渲染传入新函数：这里只在挂载时订阅一次即可（回调内部是幂等的导航）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return null;
}
