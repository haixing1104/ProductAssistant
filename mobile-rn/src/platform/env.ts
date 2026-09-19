// RN 的网络地址解析（**纯函数 + 可注入**，便于单测；也便于把"地址写错"这类问题在用例里钉死）。
//
// 为什么 RN 必须显式配地址（H5 不用）:
//   H5 的 dev 走 vite 的 `/api` 同源代理（免 CORS、免配地址），生产走边缘 nginx 同源反代；
//   RN 是原生客户端，**没有任何代理**，请求从设备直接发出 —— 地址必须写清楚。
//   而且 cookie 与 host 绑定：换了 host（例如从 localhost 换成局域网 IP）登录态就换了。
import { Platform } from "react-native";

/** Expo 在打包时会把 `EXPO_PUBLIC_*` 内联进 JS（可在 .env / 命令行设置）。 */
export type EnvLike = { EXPO_PUBLIC_API_BASE_URL?: string; EXPO_PUBLIC_DESKTOP_BASE_URL?: string };

/**
 * 解析 backend-api 基址。
 *
 * 参数:
 *   env: 环境变量表（默认取 process.env；用例注入）。
 *   os:  平台名（默认取 Platform.OS；用例注入）。
 * 返回:
 *   绝对地址，**末尾不带斜杠**（共享层会拼 `/api/v1/...`）。
 * 默认值（未配置时，仅用于本地联调）:
 *   Android 模拟器 → `http://10.0.2.2:8000`（10.0.2.2 是宿主机的 127.0.0.1）
 *   iOS 模拟器     → `http://localhost:8000`
 */
export function resolveApiBaseUrl(env: EnvLike = process.env as EnvLike, os: string = Platform.OS): string {
  const configured = (env.EXPO_PUBLIC_API_BASE_URL ?? "").trim();
  if (configured) return configured.replace(/\/+$/, "");
  return os === "android" ? "http://10.0.2.2:8000" : "http://localhost:8000";
}

/**
 * 桌面工作台地址（**深链前缀**：`navigation/RootNavigator` 的 `linking.prefixes`）。
 *
 * 2026-09 起「我的」页不再用它跳桌面端（那三个模块移动端不提供、也不给入口），
 * 当前唯一消费方就是深链 —— 生产环境需要真实域名来匹配对外分享的链接。
 * dev 默认 `http://localhost:5173`；生产同域部署时填真实域名。
 */
export function resolveDesktopBaseUrl(env: EnvLike = process.env as EnvLike): string {
  const configured = (env.EXPO_PUBLIC_DESKTOP_BASE_URL ?? "").trim();
  return configured ? configured.replace(/\/+$/, "") : "http://localhost:5173";
}
