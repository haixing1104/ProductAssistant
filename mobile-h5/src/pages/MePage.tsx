// 「我的」（移动端）：身份 + 退出登录（+ 超管的组织切换）。
//
// 为什么这里**没有**「合规词库 / 运维面板 / 用户管理」的入口（2026-09 决策）:
//   这三块属于**低频 + 表格密集**的后台操作，移动端不做页面 —— 既然不做，就**也不给入口**。
//   旧做法是把它们指向桌面端（dev 拼 `:5173`，用共享层已删除的 `desktopBaseUrl()`），
//   但那个链接在真机上是半死链：dev 的桌面端 vite 只监听 loopback（`frontend/vite.config.ts` 没开
//   `host`，dev-up 的探活也全走 127.0.0.1:5173），手机上打开 `http://<局域网IP>:5173/...` 根本连不上。
//   留着入口只会让人以为移动端"少做了功能"。要操作这三块请用桌面端工作台。
import { Button, Dialog, List, NavBar, Tag } from "antd-mobile";
import { useNavigate } from "react-router-dom";

import { authApi } from "@pa/core/api";
import { ROLE_LABEL, canApprove, useAuthStore } from "@pa/core/store/authStore";

import { forgetOrg } from "../services/rememberOrg";

import OrgScopeSection from "../components/OrgScopeSection";

/** 「我的」页：身份/角色/审批权限 + **组织切换（超管）** + 退出登录 + 清除记住的组织名。 */
export default function MePage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const clear = useAuthStore((s) => s.clear);

  const logout = () => {
    void Dialog.confirm({
      content: "确认退出登录？",
      onConfirm: async () => {
        await authApi.logout().catch(() => {}); // backend 作废 refresh 会话并清 Cookie
        clear();
        navigate("/login");
      },
    });
  };

  return (
    <div>
      <NavBar back={null}>我的</NavBar>
      <List header="账号">
        <List.Item extra={user?.username ?? "-"}>用户名</List.Item>
        <List.Item extra={user?.role ? (ROLE_LABEL[user.role] ?? user.role) : "-"}>角色</List.Item>
        <List.Item extra={<span className="pa-mono" style={{ fontSize: 12 }}>{user?.org_id ?? "-"}</span>}>
          组织 ID
        </List.Item>
        <List.Item extra={canApprove(user?.role) ? <Tag color="success">可审批</Tag> : <Tag>只读</Tag>}>
          审批权限
        </List.Item>
      </List>
      {/* 平台超管的组织切换（非超管不渲染） */}
      <OrgScopeSection />
      <div style={{ padding: 16 }}>
        <Button block color="danger" fill="outline" onClick={logout}>
          退出登录
        </Button>
        <Button
          block
          fill="none"
          color="primary"
          style={{ marginTop: 8 }}
          onClick={() => {
            forgetOrg();
            Dialog.alert({ content: "已清除本机记住的组织名（下次登录需手填）" });
          }}
        >
          清除记住的组织名
        </Button>
      </div>
    </div>
  );
}
