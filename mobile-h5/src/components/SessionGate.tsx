// 全局会话过期提示（移动版）：不静默硬跳登录页，弹窗提示后带 returnUrl 回登录页。
// 与桌面 `SessionExpiredGate` 语义一致，只换成 antd-mobile 的 `Dialog`。
import { Dialog } from "antd-mobile";
import { useEffect } from "react";

import { AUTH_EXPIRED_EVENT } from "@pa/core/services/http";

export default function SessionGate() {
  useEffect(() => {
    const show = () => {
      void Dialog.alert({
        header: null,
        title: "登录状态已过期",
        content: (
          <div style={{ fontSize: 14, lineHeight: 1.7 }}>
            <p style={{ margin: "0 0 8px" }}>您的登录状态已过期或已被吊销。为保障账号安全，请重新登录后继续操作。</p>
            <p style={{ margin: 0 }}>正在生成的内容不会被丢弃，登录后会跳回原页面。</p>
          </div>
        ),
        confirmText: "重新登录",
        onConfirm: () => window.location.assign("/login?reason=expired"),
      });
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, show);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, show);
  }, []);
  return null;
}
