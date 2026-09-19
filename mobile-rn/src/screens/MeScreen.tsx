// 「我的」（RN 版）—— 与 H5 的 `MePage` 同口径：身份 / 退出登录（+ 超管的组织切换）。
//
// 为什么这里**没有**「合规词库 / 运维面板 / 用户管理」入口（2026-09 决策，与 H5 同步）:
//   这三块属于**低频 + 表格密集**的后台操作，移动端不做页面 —— 既然不做，就也不给入口。
//   旧做法是用系统浏览器打开桌面端（`resolveDesktopBaseUrl()`，dev 默认 `http://localhost:5173`），
//   但**手机上跑原生 app 时 `localhost` 就是手机自己**，这个链接天然是死链。要操作请用桌面端工作台。
//   （`resolveDesktopBaseUrl` 本身保留：深链前缀仍在用，见 `navigation/RootNavigator` 的 `linking`。）
import { StyleSheet, View } from "react-native";

import Button from "../ui/Button";
import { ListRow, ListSection, PreWrapText } from "../ui/List";
import { NavBar } from "../ui/NavBar";
import Tag from "../ui/Tag";
import { colors, font, space } from "../ui/theme";
import { Dialog, Toast } from "../ui/feedback";
import { authApi } from "@pa/core/api";
import { ROLE_LABEL, canApprove, useAuthStore } from "@pa/core/store/authStore";

import OrgScopeSection from "../components/OrgScopeSection";
import { forgetOrg } from "../services/rememberOrg";

/** 「我的」页：身份/审批权限 + **组织切换（超管）** + 退出登录 + 清除记住的组织名。 */
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

      {/* 平台超管的组织切换（非超管不渲染） */}
      <OrgScopeSection />

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
