// 「我的」（RN 版）—— 与 H5 的 `MePage` 同口径：身份 / 退出登录 / 只在电脑端实现的模块入口。
//
// 为什么这些模块不做移动版（见 mobile-h5/README「架构决策」）:
//   运维面板（PEL/DLQ 读数）、合规词库（正则 CRUD）、用户管理属于**低频 + 表格密集**的后台操作，
//   手机上做一遍的收益远低于成本。这里给的是入口（用系统浏览器打开桌面端），而不是假装做了个残缺版本。
import { Linking, StyleSheet, Text, View } from "react-native";

import Button from "../ui/Button";
import { ListRow, ListSection, PreWrapText } from "../ui/List";
import { NavBar } from "../ui/NavBar";
import Tag from "../ui/Tag";
import { colors, font, space } from "../ui/theme";
import { Dialog, Toast } from "../ui/feedback";
import { authApi } from "@pa/core/api";
import { ROLE_LABEL, canApprove, isAdmin, useAuthStore } from "@pa/core/store/authStore";

import { resolveDesktopBaseUrl } from "../platform/env";
import { forgetOrg } from "../services/rememberOrg";

/** 只在电脑端实现的模块（低频 + 表格密集）：RN 里只给浏览器入口，不做残缺版。 */
const DESKTOP_ONLY = [
  { path: "/compliance", label: "合规词库", roles: ["admin", "reviewer"] },
  { path: "/ops", label: "运维面板", roles: ["admin"] },
  { path: "/members", label: "用户管理", roles: ["admin"] },
];

/** 「我的」页：身份/审批权限 + 退出登录 + 清除记住的组织名 + 电脑端模块的浏览器入口。 */
export default function MeScreen({ onSignedOut }: { onSignedOut: () => void }) {
  const user = useAuthStore((state) => state.user);
  const clear = useAuthStore((state) => state.clear);

  const logout = () => {
    void Dialog.confirm({
      content: "确认退出登录？",
      onConfirm: async () => {
        // backend 作废 refresh 会话并清 Cookie；失败也要让用户离开（本地状态必须清）
        await authApi.logout().catch(() => undefined);
        clear();
        onSignedOut();
      },
    });
  };

  const openDesktop = (path: string) => {
    // 桌面端页面不在本 app 里：用系统浏览器打开（**原生端没有"新标签页"概念**）
    const url = `${resolveDesktopBaseUrl()}${path}`;
    void Linking.openURL(url).catch(() =>
      Toast.show({ icon: "fail", content: `无法打开浏览器：${url}` }),
    );
  };

  const canSeeDesktop = isAdmin(user?.role) || canApprove(user?.role);

  return (
    <View style={styles.page}>
      <NavBar title="我的" back={null} />
      <ListSection header="账号">
        <ListRow label="用户名" extra={user?.username ?? "-"} />
        <ListRow label="角色" extra={user?.role ? (ROLE_LABEL[user.role] ?? user.role) : "-"} />
        <ListRow
          label="组织 ID"
          extra={<PreWrapText style={styles.mono}>{user?.org_id ?? "-"}</PreWrapText>}
        />
        <ListRow
          label="审批权限"
          extra={canApprove(user?.role) ? <Tag color="success">可审批</Tag> : <Tag>只读</Tag>}
        />
      </ListSection>

      {canSeeDesktop ? (
        <ListSection header="仅电脑端提供（点按用浏览器打开）">
          {DESKTOP_ONLY.filter((item) => item.roles.includes(user?.role ?? "")).map((item) => (
            <ListRow key={item.path} label={item.label} onPress={() => openDesktop(item.path)} />
          ))}
        </ListSection>
      ) : null}

      <View style={styles.actions}>
        <Button block variant="danger" fill="outline" size="large" onPress={logout} testID="pa-logout">
          退出登录
        </Button>
        <Button
          block
          fill="none"
          variant="primary"
          onPress={() => {
            void forgetOrg().then(() => Toast.show("已清除本机记住的组织名（下次登录需手填）"));
          }}
          style={styles.secondary}
        >
          清除记住的组织名
        </Button>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  mono: { fontSize: font.xs, color: colors.text, fontFamily: "monospace" },
  actions: { padding: space.lg },
  secondary: { marginTop: space.sm },
});
