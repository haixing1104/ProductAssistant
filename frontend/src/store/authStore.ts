// 登录态（内存版）：access token **不落 localStorage**（防 XSS 窃取）。
// 页面刷新后由 http.restoreSession() 用 HttpOnly refresh cookie 静默续签恢复。
import { create } from "zustand";

/** 当前登录身份（字段与 backend `/auth/me` 一一对应；`undefined` 表示尚未拉到）。 */
export interface AuthUser {
  id?: string;
  org_id?: string;
  username?: string;
  role?: string;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  setSession: (token: string, user?: AuthUser | null) => void;
  setUser: (user: AuthUser) => void;
  clear: () => void;
}

/**
 * 全局登录态（Zustand，单例）。
 *
 * 契约（勿改）:
 *   · `setSession(token, user?)`：续签只换 access 时**不传** `user`，避免把已有身份信息清掉；
 *   · `clear()`：登出/会话失效时同时清 token 与 user，UI 随之回登录页（由守卫跳转）。
 */
export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,
  setSession: (token, user) => {
    const patch: { token: string; user?: AuthUser | null } = { token };
    if (user !== undefined) patch.user = user;
    set(patch);
  },
  setUser: (user) => set({ user }),
  clear: () => set({ token: null, user: null }),
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
