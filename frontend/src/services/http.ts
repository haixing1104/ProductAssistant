// 统一 axios 实例（与 backend 的会话契约逐条对齐）：
//   · access token **仅内存**，请求自动带 Bearer；
//   · refresh token 走 HttpOnly Cookie（withCredentials），且 refresh 必须带
//     `X-Requested-With: fetch`（backend 的 CSRF 防护，缺了直接 403 —— 见 auth_router.refresh）；
//   · 401 自动静默续签并**重放原请求**（单飞防并发）；
//   · 到期前主动续签（scheduleTokenRefresh，解 JWT exp）；
//   · fetchStreamTicket：为 SSE 换短时票据（长连接与短时效 access 解耦）。
import axios, { type AxiosRequestConfig } from "axios";

import { useAuthStore } from "../store/authStore";

import { AUTH_EXPIRED_EVENT, apiUrl, getPlatform } from "./platform";

/**
 * 全局 axios 实例（唯一出口，业务层只用 `api/index.ts` 封装的域方法）。
 *
 * 基址**在请求拦截器里现算**（见下方注释）：不能在 `create()` 里定死 ——
 * RN 的平台实现可能晚于本模块 import 才装上，写死会退化成空串（相对 URL 在原生端无效）。
 */
export const http = axios.create({
  // 基址在请求拦截器里按当前平台算（见下方 setBaseURL 注释）：
  // Web = ""（同源相对路径，dev 走 vite 代理）；RN = EXPO_PUBLIC_API_BASE_URL（原生没有代理）
  timeout: 30000,
  withCredentials: true,
});

/** 会话过期全局事件：SessionExpiredGate 监听并弹友好提示（不静默硬跳）。常量定义在 platform.ts。 */
export { AUTH_EXPIRED_EVENT };

/** backend refresh 的 CSRF 头（服务端要求 `x-requested-with: fetch`） */
export const CSRF_HEADER = { "X-Requested-With": "fetch" };

/**
 * 生成本次请求的关联 ID（`X-Request-Id`）。
 *
 * 用途：一次「生成」要跨 backend → Redis → ai-engine 三段日志，靠时间戳猜时序很痛苦；
 * 带上这个 ID 后，backend 会把它写进 job 载荷并一路透传到 worker/result 日志。
 * 优先用 `crypto.randomUUID()`（HTTPS/现代浏览器都有），退化到随机串（老内核/测试环境）。
 */
export function newRequestId(): string {
  // 平台端口：Web 用 crypto.randomUUID（退化到 req- 前缀）；RN 用 expo-crypto
  return getPlatform().newRequestId();
}

http.interceptors.request.use((config) => {
  // 每次请求都按当前平台算基址：避免「平台实现在 import 之后才装上」时把基址定死成空串
  config.baseURL = apiUrl("/api/v1");
  const token = useAuthStore.getState().token;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  // 同一请求的重放（401 续签后重发）复用同一个 ID：config.headers 会被带走，
  // 若没有就补一个（首次请求）。401 重放路径见下方 retryable。
  if (!config.headers["X-Request-Id"]) config.headers["X-Request-Id"] = newRequestId();
  return config;
});

// ---------- 静默续签（单飞） ----------
let refreshing: Promise<boolean> | null = null;

/** 真正发续签请求（**只应由 `refreshSession` 调用**，以保住单飞语义）。 */
async function rawRefresh(): Promise<boolean> {
  try {
    const resp = await http.post("/auth/refresh", null, { headers: CSRF_HEADER });
    const access = (resp.data as { data?: { access_token?: string } }).data?.access_token;
    if (!access) return false;
    useAuthStore.getState().setSession(access);
    scheduleTokenRefresh();
    return true;
  } catch {
    return false;
  }
}

/**
 * 静默续签（单飞）。
 *
 * 参数:
 *   force: ``false``（默认）= 仅当本地没有 access 时续签（会话恢复场景）；
 *          ``true`` = 即使本地有 access 也续签（**到期前主动续签**场景）。
 * 返回:
 *   ``true`` = 现在持有可用 access；``false`` = 续签失败（调用方决定提示或重试）。
 * 注意:
 *   ``force`` 这个参数不是可有可无的：到期前的定时器若走默认分支，会因「本地尚有 token」
 *   直接返回 true 而**什么都不做**，把「主动续签」变成死代码（只剩 401 兜底）—— 实测踩过。
 */
export function refreshSession(force = false): Promise<boolean> {
  if (!force && useAuthStore.getState().token) return Promise.resolve(true);
  if (!refreshing) {
    refreshing = rawRefresh().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

/** 应用启动/401 兜底：用 HttpOnly refresh cookie 换新 access（并发只发一次请求）。 */
export function restoreSession(): Promise<boolean> {
  return refreshSession(false);
}

// ---------- 会话过期（友好提示，非硬跳转） ----------
let expiredNotified = false;

/** 记录"从哪来"（委托平台端口：Web 写 sessionStorage，RN 写 AsyncStorage）。 */
function rememberReturnUrl(): void {
  // 原逻辑逐行搬进 platform.ts，这里只做转发（见上方注释）
  getPlatform().rememberReturnUrl();
}

/** 登录成功/登出后可再次触发过期提示时复位标记。 */
export function resetExpiredFlag(): void {
  expiredNotified = false;
}

/** 会话真实失效（续签失败/被吊销）：清空登录态并通知 UI，由用户确认后再离开。 */
export function sessionExpired(): void {
  // 注意：这里**不再**用 `typeof window === "undefined"` 提前返回 ——
  // RN 里 window 未必存在，提前返回会让「清空登录态」这一步在原生端静默丢失。
  // 平台差异改由端口承担（Web = window 事件；RN = 内部 emitter）。
  rememberReturnUrl();
  useAuthStore.getState().clear();
  if (expiredNotified) return;
  expiredNotified = true;
  getPlatform().emitAuthExpired();
}

// ---------- 到期前主动续签 ----------
let cancelAutoTimer: (() => void) | undefined;

/** 解码 JWT exp（由平台端口提供：Web 用 atob，RN 用纯 JS base64url 解码），提前 ~30s 主动续签。 */
export function scheduleTokenRefresh(): void {
  const platform = getPlatform();
  const token = useAuthStore.getState().token;
  if (!token) return;
  const expMs = platform.jwtExpMs(token);
  if (expMs === null) return; // 畸形 token：不排定时器（只留 401 兜底），也不抛错
  const delay = Math.max(0, expMs - Date.now() - 30000);
  cancelAutoTimer?.();
  cancelAutoTimer = platform.setTimeout(async () => {
    const ok = await refreshSession(true); // force：到期前必须真续签，不能因「本地还有 token」跳过
    if (!ok && useAuthStore.getState().token) {
      // 5s 后重试一次（避免瞬时网络抖动误踢）；仍失败则主动弹「会话过期」提示
      platform.setTimeout(async () => {
        const ok2 = await refreshSession(true);
        if (!ok2 && useAuthStore.getState().token) sessionExpired();
      }, 5000);
    }
  }, delay);
}

// ---------- SSE 短时票据 ----------
/** 用 Bearer access 换取商品流的短时签名票据（默认 120s）。失败返回 null（401 已走全局续签/过期流程）。 */
export async function fetchStreamTicket(productId: string): Promise<string | null> {
  try {
    const resp = await http.get(`/products/${productId}/stream-ticket`);
    const ticket = (resp.data as { data?: { ticket?: string } }).data?.ticket;
    return ticket ?? null;
  } catch {
    return null;
  }
}

// ---------- 401 兜底：先续签一次再重放，失败走友好过期 ----------
type RetryableConfig = AxiosRequestConfig & { _retried?: boolean };

http.interceptors.response.use(
  (resp) => resp,
  async (error) => {
    const status = (error as { response?: { status?: number } }).response?.status;
    const config = (error as { config?: RetryableConfig }).config;
    const url = config?.url ?? "";
    if (status === 401 && !url.includes("/auth/login") && !url.includes("/auth/refresh") && !config?._retried) {
      config!._retried = true;
      const ok = await restoreSession();
      if (ok) return http.request(config!);
    }
    if (status === 401 && !url.includes("/auth/login")) {
      sessionExpired();
    }
    return Promise.reject(error);
  },
);
