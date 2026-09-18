// 记住组织名（移动端专属便利，桌面端没做这件事）。
//
// 背景: PA 的用户名只在组织内唯一，跨组织同名账号必须传 `org_name` 消歧；
// 手机上每次手打企业名体验很差，所以在**登录成功后**记住它，下次自动带出。
// 注意: 只存"组织名"这一个非敏感字段（**绝不存密码/token** —— access 仍在内存、refresh 仍在 HttpOnly cookie）。
const ORG_KEY = "pa_mobile_org";

/** 安全取 storage：隐私模式/禁用 localStorage 时返回 null，调用方退化为"没记住"。 */
function safeStorage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** 读取记住的组织名（storage 不可用时返回空串 = 没记住，不抛错）。 */
export function readRememberedOrg(storage: Storage | null = safeStorage()): string {
  try {
    return storage?.getItem(ORG_KEY) ?? "";
  } catch {
    return "";
  }
}

/** 记住组织名（只在登录成功后调用；空白值忽略，写入失败不影响登录流程）。 */
export function rememberOrg(org: string, storage: Storage | null = safeStorage()): void {
  const value = org.trim();
  if (!value) return;
  try {
    storage?.setItem(ORG_KEY, value);
  } catch {
    // 存不进去也不影响登录流程
  }
}

/** 清除记住的组织名（「我的」页的手动清理入口）。 */
export function forgetOrg(storage: Storage | null = safeStorage()): void {
  try {
    storage?.removeItem(ORG_KEY);
  } catch {
    // 忽略
  }
}
