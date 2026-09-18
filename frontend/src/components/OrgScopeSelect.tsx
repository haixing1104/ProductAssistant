// 组织选择器（**仅平台超管可见**）：决定所有请求的「当前租户」（`X-Org-Id` 头）。
//
// 为什么需要它:
//   本系统按 `org_id` 隔离数据（backend `repositories` 层强制过滤）。超管能操作「所有租户的
//   数据」，机制是**先选一个租户**，再由后端 `core/deps` 把「当前租户」覆盖成它 ——
//   服务端刻意**不提供**「一次返回全部租户」的混合视图（那要放宽 28 处过滤点，越权面太大）。
//   因此这个下拉是权威入口：不选 = 只看到自己组织的（通常为空）数据。
//
// 切换后的刷新: 由父布局用 `key={selectedOrgId}` **重挂载**内容区实现（组件内部不动数据流），
//   这样每个页面本来就有的「挂载时取数」逻辑自然重跑，不需要各页面自己监听选择变化。
import { Select, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";

import { orgApi } from "../api";
import { canSwitchOrg, selectableOrgs } from "../services/orgScope";
import { useAuthStore } from "../store/authStore";
import type { Org } from "../types/api";

/** 超管的组织下拉（非超管渲染 null，不影响既有界面）。 */
export default function OrgScopeSelect() {
  const user = useAuthStore((s) => s.user);
  const selectedOrgId = useAuthStore((s) => s.selectedOrgId);
  const setSelectedOrg = useAuthStore((s) => s.setSelectedOrg);
  const [orgs, setOrgs] = useState<Org[]>([]);
  const enabled = canSwitchOrg(user);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    // 列表失败不阻塞工作台：选择器降级为「只有自己的组织」不显示额外选项（不弹错、不影响其它请求）
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
    <Space>
      <Typography.Text type="secondary">当前组织</Typography.Text>
      <Select
        value={selectedOrgId ?? undefined}
        placeholder="请选择组织"
        style={{ minWidth: 220 }}
        // 只列启用中的组织：已停用组织选了必然 400（后端会拒），不如不出现
        options={selectableOrgs(orgs).map((org) => ({ value: org.id, label: org.name }))}
        onChange={(value: string) => setSelectedOrg(value)}
      />
      {selectedOrgId ? null : <Tag color="warning">未选择组织：当前仅显示本组织数据</Tag>}
    </Space>
  );
}
