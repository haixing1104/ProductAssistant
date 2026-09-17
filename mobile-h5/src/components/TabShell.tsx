// 底部 Tab 外壳（商品 / 审批 / 我的）。
//
// 与桌面 `AppLayout` 的对应关系: 桌面用左侧 Menu + 顶部身份条；移动端用底部 TabBar，
// 身份与退出登录收进「我的」（手机顶部空间要留给页面标题与主操作）。
// 角色显隐口径与后端 RBAC 一一对应（复用共享层 canApprove / isAdmin，避免与后端漂移）。
import { SafeArea, TabBar } from "antd-mobile";
import { AppOutline, UnorderedListOutline, UserOutline } from "antd-mobile-icons";
import { useEffect } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { authApi } from "@pa/core/api";
import { canApprove, useAuthStore } from "@pa/core/store/authStore";

export default function TabShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);

  // 刷新后 token 在内存里丢了、但 HttpOnly refresh cookie 仍有效（AuthGuard 已续签）；
  // 此时从 /auth/me 取权威身份 —— 角色决定 Tab 显隐，不能靠猜。
  useEffect(() => {
    if (token && !user?.username) {
      authApi
        .me()
        .then((me) => setUser({ id: me.id, org_id: me.org_id, username: me.username, role: me.role }))
        .catch(() => {});
    }
  }, [token, user, setUser]);

  const activeKey = location.pathname.startsWith("/approvals")
    ? "/approvals"
    : location.pathname.startsWith("/me")
      ? "/me"
      : "/";

  return (
    <div className="pa-page" style={{ paddingBottom: "calc(50px + var(--pa-safe-bottom))" }}>
      <Outlet />
      <div style={{ position: "fixed", bottom: 0, left: 0, right: 0, background: "#fff", zIndex: 100 }}>
        <TabBar activeKey={activeKey} onChange={(key) => navigate(key)}>
          <TabBar.Item key="/" icon={<AppOutline />} title="商品" />
          {canApprove(user?.role) ? <TabBar.Item key="/approvals" icon={<UnorderedListOutline />} title="审批" /> : null}
          <TabBar.Item key="/me" icon={<UserOutline />} title="我的" />
        </TabBar>
        {/* 顶部/底部安全区：避免 iPhone 横条压住 TabBar */}
        <SafeArea position="bottom" />
      </div>
    </div>
  );
}
