// 组织选择区块（**仅平台超管可见**）：决定所有请求的「当前租户」（`X-Org-Id` 头）。
//
// 为什么放在「我的」页而不是主页面顶部（2026-09 调整，与 H5 同步）:
//   它属于**账号级**设置（与身份 / 退出登录同类），常驻在所有 Tab 顶部会吃掉手机最宝贵的
//   一屏空间，而运营在商品/审批页关心的是数据本身，不是"我在哪个租户"。
//
// 三端对应: 桌面 = 右上角身份区旁的下拉（那是桌面版的「我的」）；
//   H5 = `List` + `Selector`（mobile-h5/src/components/OrgScopeSection.tsx）；
//   RN = 本文件（`ListSection` + `Chips`，与「我的」页其它区块同款）。
//
// ⚠️ RN 与 H5 的关键差异（改这里前先看）:
//   `createBottomTabNavigator` **默认保活已访问过的 Tab 屏**，所以「在『我的』切了租户 →
//   回商品 Tab」不会自动重新取数。刷新由 `TabShell` 的 `key={selectedOrgId}` 整体重挂载完成
//   —— 那个 key **不能删**（H5 用平级路由会卸载重挂载，所以 H5 不需要 key）。
import { useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";

import Chips from "../ui/Chips";
import { ListSection } from "../ui/List";
import { colors, font, space } from "../ui/theme";
import { orgApi } from "@pa/core/api";
import { canSwitchOrg, selectableOrgs } from "@pa/core/services/orgScope";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Org } from "@pa/core/types/api";

/** 超管的组织选择区块（非超管渲染 null ⇒「我的」页保持原样）。 */
export default function OrgScopeSection() {
  const user = useAuthStore((state) => state.user);
  const selectedOrgId = useAuthStore((state) => state.selectedOrgId);
  const setSelectedOrg = useAuthStore((state) => state.setSelectedOrg);
  const [orgs, setOrgs] = useState<Org[]>([]);
  const enabled = canSwitchOrg(user);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    // 列表失败不阻塞页面：只是没有可选项（不影响其它请求，也不弹错）
    orgApi
      .list()
      .then((rows) => {
        if (alive) setOrgs(rows);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [enabled]);

  if (!enabled) return null;

  return (
    <ListSection header="当前组织（超管可切换）" testID="pa-org-scope">
      <View style={styles.body}>
        <Chips
          options={selectableOrgs(orgs).map((org) => ({ label: org.name, value: org.id }))}
          value={selectedOrgId ?? ""}
          onChange={(value) => setSelectedOrg(value || null)}
          scrollable
        />
        {selectedOrgId ? null : (
          <Text style={styles.hint}>未选择：当前仅显示本组织数据</Text>
        )}
      </View>
    </ListSection>
  );
}

const styles = StyleSheet.create({
  body: { paddingVertical: space.xs },
  hint: { fontSize: font.xs, color: colors.warning, paddingHorizontal: space.md },
});
