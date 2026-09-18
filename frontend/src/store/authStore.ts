// 登录态（内存版）：access token **不落 localStorage**（防 XSS 窃取）。
// 页面刷新后由 http.restoreSession() 用 HttpOnly refresh cookie 静默续签恢复。
import { create } from "zustand";

import type { CurrentUser } from "../types/api";

/** 当前登录身份（字段与 backend `/auth/me` 一一对应；`undefined` 表示尚未拉到）。 */
export interface AuthUser {
  id?: string;
  org_id?: string;
  username?: string;
  role?: string;
  /** 平台超管标记（`/auth/me` 的 `is_superuser`）：决定是否显示组织选择器。 */
  is_superuser?: boolean;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  /**
   * 平台超管选中的目标组织（决定请求头 `X-Org-Id`；非超管恒为 null）。
   *
   * 为什么放在登录态里而不是各页面局部 state:
   *   它必须是**全局单值** —— 所有请求（列表/详情/写操作/SSE 换票）都要带同一个租户，
   *   否则会出现「列表是 A 租户、详情取的是 B 租户」这类串租户错乱。
   *   刻意**不落盘**（与 access token 同为会话级）：刷新后重新选择，
   *   避免「上次选了谁」被静默沿用而误操作。
   */
  selectedOrgId: string | null;
  setSession: (token: string, user?: AuthUser | null) => void;
  setUser: (user: AuthUser) => void;
  setSelectedOrg: (orgId: string | null) => void;
  clear: () => void;
}

/**
 * 全局登录态（Zustand，单例）。
 *
 * 契约（勿改）:
 *   · `setSession(token, user?)`：续签只换 access 时**不传** `user`，避免把已有身份信息清掉；
 *   · `clear()`：登出/会话失效时同时清 token 与 user，UI 随之回登录页（由守卫跳转），
 *     并**一并清掉选中的组织**（换个人登录不该继承上一位超管的租户选择）。
 */
export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,
  selectedOrgId: null,
  setSession: (token, user) => {
    const patch: { token: string; user?: AuthUser | null } = { token };
    if (user !== undefined) patch.user = user;
    set(patch);
  },
  setUser: (user) => set({ user }),
  setSelectedOrg: (orgId) => set({ selectedOrgId: orgId }),
  clear: () => set({ token: null, user: null, selectedOrgId: null }),
}));

/** 角色中文名（与 backend RBAC 的三角色一致：admin / reviewer / operator）。 */
export const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  reviewer: "审核员",
  operator: "运营",
};

/** 是否具备审批资格（与 backend APPROVER_ROLES 对齐：operator 不可审批）。 */
export function canApprove(role?: string | null): boolean {
  return role === "admin" || role === "reviewer";
}

/** 是否可写商品域（与 backend WRITE_ROLES 对齐）。 */
export function canWriteProducts(role?: string | null): boolean {
  return role === "admin" || role === "operator";
}

/** 是否管理员（危险写：彻底删除 / 合规词库写 / 运维面）。 */
export function isAdmin(role?: string | null): boolean {
  return role === "admin";
}

/**
 * `/auth/me` → 登录态身份（**唯一映射点**）。
 *
 * 为什么抽出来: 三端各有 1~2 处「登录成功后 / 刷新后补身份」都要做同样的字段搬运，
 * 各写一份的结局是「新增身份字段漏了一端」——例如超管标记只在桌面端生效、手机端
 * 不显示组织选择器。集中一处后，加字段只改这里。
 */
export function authUserFromMe(me: CurrentUser): AuthUser {
  return {
    id: me.id,
    org_id: me.org_id,
    username: me.username,
    role: me.role,
    is_superuser: me.is_superuser,
  };
}
