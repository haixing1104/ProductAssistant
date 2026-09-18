// 记住组织名（RN 版）—— 与 H5 的 `mobile-h5/src/services/rememberOrg.ts` **同一份业务口径**，
// 但存储介质不同：H5 用 localStorage（同步），RN 用 AsyncStorage（**异步**）。
//
// 背景: PA 的用户名只在组织内唯一，跨组织同名账号必须传 `org_name` 消歧；
// 手机上每次手打企业名体验很差，所以在**登录成功后**记住它、下次自动带出。
// 注意: 只存"组织名"这一个非敏感字段（**绝不存密码/token** —— access 仍在内存、refresh 仍在实现层的 cookie 存储）。
import AsyncStorage from "@react-native-async-storage/async-storage";

/** AsyncStorage 的键名（与 H5/localStorage 同名：排查时一眼能对上）。 */
const ORG_KEY = "pa_mobile_org";

/** 读取记住的组织名（读不到/异常都返回空串，调用方退化为"没记住"）。 */
export async function readRememberedOrg(): Promise<string> {
  try {
    return (await AsyncStorage.getItem(ORG_KEY)) ?? "";
  } catch {
    return "";
  }
}

/** 记住组织名（空白不写；写失败也不影响登录流程）。 */
export async function rememberOrg(org: string): Promise<void> {
  const value = org.trim();
  if (!value) return;
  try {
    await AsyncStorage.setItem(ORG_KEY, value);
  } catch {
    // 存不进去不影响登录
  }
}

/** 清除记住的组织名。 */
export async function forgetOrg(): Promise<void> {
  try {
    await AsyncStorage.removeItem(ORG_KEY);
  } catch {
    // 忽略
  }
}
