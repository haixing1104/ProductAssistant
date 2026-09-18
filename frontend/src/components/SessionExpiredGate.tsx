// 全局会话过期提示：续签失败不静默硬跳登录页，而是弹友好提示，
// 由用户确认后带 returnUrl 回登录页；登录成功后跳回原页。
import { Modal } from "antd";
import { useEffect, useState } from "react";

import { AUTH_EXPIRED_EVENT } from "../services/http";

/** 会话过期弹窗宿主（挂一次即可）：订阅 `AUTH_EXPIRED_EVENT`，确认后带 `reason=expired` 回登录页。 */
export default function SessionExpiredGate() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener(AUTH_EXPIRED_EVENT, show);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, show);
  }, []);

  const goLogin = () => {
    setOpen(false);
    window.location.assign("/login?reason=expired");
  };

  return (
    <Modal
      open={open}
      title="登录状态已过期"
      okText="重新登录"
      closable={false}
      maskClosable={false}
      onOk={goLogin}
    >
      <p>您的登录状态已过期或已被吊销。为保障账号安全，请重新登录后继续操作；</p>
      <p>已生成/正在生成的内容不会被丢弃，登录后将跳回原页面。</p>
    </Modal>
  );
}
