// 组织选择区块（**仅平台超管可见**）：决定所有请求的「当前租户」（`X-Org-Id` 头）。
//
// 为什么放在「我的」页而不是主页面顶部（2026-09 调整）:
//   它属于**账号级**设置（与身份 / 退出登录同类），不是每次打开都要动的操作；常驻在列表页
//   顶部会吃掉手机最宝贵的一屏空间，而运营在商品/审批页关心的是数据本身，不是"我在哪个租户"。
//
// 与桌面端的差异: 桌面用右上角身份区旁的下拉（那是桌面版的「我的」）；
//   H5 用本区块（antd-mobile `List` 内嵌 `Selector` 芯片 —— 手机上既没有下拉空间，
//   也不适合把 antd 的 Select 塞进移动端布局）。
//
// 语义（三端一致）: 不选 = 只看到自己组织的（通常为空）数据；服务端会校验目标组织存在且启用，
//   否则 400。切换后回到商品/审批 Tab 会重新挂载取数（见 `TabShell`）。
import { List, Selector, Toast } from "antd-mobile";
import { useEffect, useState } from "react";

import { orgApi } from "@pa/core/api";
import { canSwitchOrg, selectableOrgs } from "@pa/core/services/orgScope";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Org } from "@pa/core/types/api";

/** 超管的组织选择区块（非超管渲染 null ⇒「我的」页保持原样）。 */
export default function OrgScopeSection() {
  const user = useAuthStore((s) => s.user);
  const selectedOrgId = useAuthStore((s) => s.selectedOrgId);
  const setSelectedOrg = useAuthStore((s) => s.setSelectedOrg);
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

  const options = selectableOrgs(orgs).map((org) => ({ label: org.name, value: org.id }));

  return (
    <List header="当前组织（超管可切换）">
      <List.Item>
        <Selector
          options={options}
          value={selectedOrgId ? [selectedOrgId] : []}
          onChange={(values) => {
            const next = (values[0] as string) ?? null;
            setSelectedOrg(next);
            const label = options.find((item) => item.value === next)?.label;
            if (next && label) Toast.show({ content: `已切换到「${label}」` });
          }}
        />
        {selectedOrgId ? null : (
          <div style={{ marginTop: 8, fontSize: 12, color: "#ff8f1f" }}>
            未选择：当前仅显示本组织数据
          </div>
        )}
      </List.Item>
    </List>
  );
}
