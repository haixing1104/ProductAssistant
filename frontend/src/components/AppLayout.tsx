// 左侧菜单 + 顶部身份 + 内容区（antd Layout）。
//
// 菜单按角色显隐（与 backend RBAC 一一对应，改这里前先看 routers 里的 require_roles）：
//   · 商品管理 → 三种角色都能进（写操作在页面内按角色禁用）；
//   · 审批中心 → admin/reviewer（operator 不可审批）；
//   · 合规词库 → admin/reviewer 可读（写按钮仅 admin 显示）；
//   · 运维面板 / 用户管理 → 仅 admin。
import { Alert, Button, Layout, Menu, Space, Tag, Typography } from "antd";
import { useEffect } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { authApi } from "../api";
import { ROLE_LABEL, canApprove, isAdmin, useAuthStore } from "../store/authStore";

const { Content, Header, Sider } = Layout;

/** 菜单键 → 路由前缀（用于高亮当前项） */
const MENU_KEYS: Array<{ key: string; match: (path: string) => boolean }> = [
  { key: "/", match: (p) => p === "/" || p.startsWith("/products") },
  { key: "/approvals", match: (p) => p.startsWith("/approvals") },
  { key: "/compliance", match: (p) => p.startsWith("/compliance") },
  { key: "/ops", match: (p) => p.startsWith("/ops") },
  { key: "/members", match: (p) => p.startsWith("/members") },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const clear = useAuthStore((s) => s.clear);

  // 刷新后 token 存在但本地身份缺失时，从 /auth/me 取权威身份（角色决定菜单）
  useEffect(() => {
    if (token && !user?.username) {
      authApi
        .me()
        .then((me) => setUser({ id: me.id, org_id: me.org_id, username: me.username, role: me.role }))
        .catch(() => {});
    }
  }, [token, user, setUser]);

  const identity = user?.username
    ? `${user.username} · ${(user.role && ROLE_LABEL[user.role]) || user.role}`
    : "加载身份中…";

  const menuItems = [
    { key: "/", label: <Link to="/">商品管理</Link> },
    ...(canApprove(user?.role) ? [{ key: "/approvals", label: <Link to="/approvals">审批中心</Link> }] : []),
    ...(canApprove(user?.role) ? [{ key: "/compliance", label: <Link to="/compliance">合规词库</Link> }] : []),
    ...(isAdmin(user?.role) ? [{ key: "/ops", label: <Link to="/ops">运维面板</Link> }] : []),
    ...(isAdmin(user?.role) ? [{ key: "/members", label: <Link to="/members">用户管理</Link> }] : []),
  ];
  const selected = MENU_KEYS.find((item) => item.match(location.pathname))?.key ?? "/";

  const logout = () => {
    void authApi.logout().catch(() => {}); // backend 作废 refresh 会话并清 Cookie
    clear();
    navigate("/login");
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider theme="dark" width={200}>
        <div style={{ color: "#fff", padding: 16, fontSize: 16, fontWeight: 600 }}>ProductAssistant</div>
        <Menu theme="dark" mode="inline" selectedKeys={[selected]} items={menuItems} />
      </Sider>
      <Layout>
        <Header
          style={{ background: "#fff", display: "flex", justifyContent: "space-between", alignItems: "center" }}
        >
          <Typography.Text strong>电商内容供应链工作台</Typography.Text>
          <Space>
            <Typography.Text type="secondary">{identity}</Typography.Text>
            <Button type="link" onClick={logout}>
              退出登录
            </Button>
          </Space>
        </Header>
        <Content style={{ margin: 16, padding: 16, background: "#fff" }}>
          {user?.role && !canApprove(user.role) && location.pathname.startsWith("/approvals") ? (
            <Alert type="warning" showIcon message="当前角色无审批权限" style={{ marginBottom: 12 }} />
          ) : null}
          {user?.role ? <Tag style={{ marginBottom: 12 }}>角色：{(ROLE_LABEL[user.role] ?? user.role)}</Tag> : null}
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
