// 「我的」（移动端）：身份 + 退出登录 + 只在电脑端实现的模块入口。
//
// 为什么这些模块不做移动版（A 档范围，见 README「架构决策」）:
//   运维面板（PEL/DLQ 读数）、合规词库（正则 CRUD）、用户管理属于**低频 + 表格密集**的
//   后台操作，手机上做一遍的收益远低于成本；移动端的价值集中在「审批」这类"人不在电脑前"的事。
//   这里给的是入口（dev 指向 :5173，生产同源 /），而不是假装做了个残缺版本。
import { Button, Dialog, List, NavBar, Tag } from "antd-mobile";
import { useNavigate } from "react-router-dom";

import { authApi } from "@pa/core/api";
import { ROLE_LABEL, canApprove, isAdmin, useAuthStore } from "@pa/core/store/authStore";

import { desktopBaseUrl } from "../services/format";
import { forgetOrg } from "../services/rememberOrg";

const DESKTOP_ONLY = [
  { path: "/compliance", label: "合规词库", roles: ["admin", "reviewer"] },
  { path: "/ops", label: "运维面板", roles: ["admin"] },
  { path: "/members", label: "用户管理", roles: ["admin"] },
];

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

  const base = desktopBaseUrl(window.location);
  const openDesktop = (path: string) => {
    // 桌面端页面不存在于本 app：新开一个标签，避免把移动端 SPA 的路由状态打乱
    window.open(`${base}${path}`, "_blank", "noopener");
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
      {isAdmin(user?.role) || canApprove(user?.role) ? (
        <List header="仅电脑端提供（点按在桌面端打开）">
          {DESKTOP_ONLY.filter((item) => item.roles.includes(user?.role ?? "")).map((item) => (
            <List.Item key={item.path} clickable arrowIcon onClick={() => openDesktop(item.path)}>
              {item.label}
            </List.Item>
          ))}
        </List>
      ) : null}
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
