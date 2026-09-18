// 组织范围（租户切换）：平台超管的「当前租户」契约。
//
// 为什么单独一个文件:
//   header 名与「谁能用」这一条口径，被 `http.ts`（发请求）与三个端的选择器 UI
//   同时依赖；写在页面里会出现「改了 UI 忘了改拦截器」这类静默失效（表现为选完组织
//   数据没变）。后端契约见 `backend-api core/deps.SUPERUSER_ORG_HEADER`。
//
// 语义:
//   · **只有超管**能带这个头；普通用户带了会被服务端静默忽略（不报错、不生效）；
//   · 未选组织 / 非超管 → 不发头，请求仍在「归属组织」里执行（旧行为，零影响）；
//   · 服务端会校验目标组织存在且 `status='active'`，否则 400。
import { useAuthStore } from "../store/authStore";
import type { CurrentUser, Org } from "../types/api";

/** 平台超管切换当前租户的请求头（与后端 `SUPERUSER_ORG_HEADER` 严格一致）。 */
export const ORG_SCOPE_HEADER = "X-Org-Id";

/** 是否具备跨租户切换能力（仅平台超管）。 */
export function canSwitchOrg(user?: Pick<CurrentUser, "is_superuser"> | null): boolean {
  return user?.is_superuser === true;
}

/** 本次请求应携带的目标组织（非超管或未选择时为 null = 不发头）。 */
export function currentOrgScope(): string | null {
  const { user, selectedOrgId } = useAuthStore.getState();
  return canSwitchOrg(user) && selectedOrgId ? selectedOrgId : null;
}

/** 可选组织（只保留启用状态；已停用组织出现在列表里但不可选，避免选了必然 400）。 */
export function selectableOrgs(orgs: Org[]): Org[] {
  return orgs.filter((org) => org.status === "active");
}

/** 组织名（找不到时回退 id，界面不出现空白）。 */
export function orgLabel(orgs: Org[], orgId?: string | null): string {
  if (!orgId) return "";
  return orgs.find((org) => org.id === orgId)?.name ?? orgId;
}
