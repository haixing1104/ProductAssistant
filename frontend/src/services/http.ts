// 统一 axios 实例（与 backend 的会话契约逐条对齐）：
//   · access token **仅内存**，请求自动带 Bearer；
//   · refresh token 走 HttpOnly Cookie（withCredentials），且 refresh 必须带
//     `X-Requested-With: fetch`（backend 的 CSRF 防护，缺了直接 403 —— 见 auth_router.refresh）；
//   · 401 自动静默续签并**重放原请求**（单飞防并发）；
//   · 到期前主动续签（scheduleTokenRefresh，解 JWT exp）；
//   · fetchStreamTicket：为 SSE 换短时票据（长连接与短时效 access 解耦）。
import axios, { type AxiosRequestConfig } from "axios";

import { useAuthStore } from "../store/authStore";

export const http = axios.create({
  baseURL: "/api/v1",
  // 30s：CSV 导入/生成触发都在秒级；SSE 不走 axios（见 services/sse.ts）
  timeout: 30000,
  withCredentials: true,
});

/** 会话过期全局事件：SessionExpiredGate 监听并弹友好提示（不静默硬跳） */
export const AUTH_EXPIRED_EVENT = "pa:auth-expired";

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
  const cryptoObj = globalThis.crypto as Crypto | undefined;
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
  return `req-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
}

http.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  // 同一请求的重放（401 续签后重发）复用同一个 ID：config.headers 会被带走，
  // 若没有就补一个（首次请求）。401 重放路径见下方 retryable。
  if (!config.headers["X-Request-Id"]) config.headers["X-Request-Id"] = newRequestId();
  return config;
});

// ---------- 静默续签（单飞） ----------
let refreshing: Promise<boolean> | null = null;

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

function rememberReturnUrl(): void {
  try {
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      sessionStorage.setItem("pa_return", window.location.pathname + window.location.search);
    }
  } catch {
    // sessionStorage 不可用（隐私模式等）时忽略
  }
}

/** 登录成功/登出后可再次触发过期提示时复位标记。 */
export function resetExpiredFlag(): void {
  expiredNotified = false;
}

/** 会话真实失效（续签失败/被吊销）：清空登录态并通知 UI，由用户确认后再离开。 */
export function sessionExpired(): void {
  if (typeof window === "undefined") return;
  rememberReturnUrl();
  useAuthStore.getState().clear();
  if (expiredNotified) return;
  expiredNotified = true;
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
}

// ---------- 到期前主动续签 ----------
let autoTimer: number | undefined;

/** 解码 JWT exp，提前 ~30s 主动续签；续签成功后再排下一次。 */
export function scheduleTokenRefresh(): void {
  if (typeof window === "undefined") return;
  const token = useAuthStore.getState().token;
  if (!token) return;
  let expMs = 0;
  try {
    const part = token.split(".")[1];
    // JWT 的 base64url **不带填充**，而 atob 在部分运行时（Node/jsdom）要求长度对齐 4 ——
    // 不补 `=` 会直接抛 InvalidCharacterError，导致「到期前续签」静默失效（只剩 401 兜底）。
    const base64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
    const json = decodeURIComponent(escape(atob(padded)));
    expMs = (JSON.parse(json).exp ?? 0) * 1000;
  } catch {
    return;
  }
  const delay = Math.max(0, expMs - Date.now() - 30000);
  if (autoTimer !== undefined) window.clearTimeout(autoTimer);
  autoTimer = window.setTimeout(async () => {
    const ok = await refreshSession(true); // force：到期前必须真续签，不能因「本地还有 token」跳过
    if (!ok && useAuthStore.getState().token) {
      // 5s 后重试一次（避免瞬时网络抖动误踢）；仍失败则主动弹「会话过期」提示
      window.setTimeout(async () => {
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
