// 登录态（内存版）：access token **不落 localStorage**（防 XSS 窃取）。
// 页面刷新后由 http.restoreSession() 用 HttpOnly refresh cookie 静默续签恢复。
import { create } from "zustand";

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
