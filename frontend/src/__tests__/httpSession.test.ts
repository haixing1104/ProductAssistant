// 会话层用例：access 仅内存 / 401 单飞续签与重放 / 续签失败走友好过期 / 主动续签排程。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "../store/authStore";

// 用最小 axios 替身隔离被测逻辑（不发真实请求）
const post = vi.fn();
const request = vi.fn();
vi.mock("axios", async () => {
  const actual = await vi.importActual<typeof import("axios")>("axios");
  const instance = {
    post: (...args: unknown[]) => post(...args),
    request: (...args: unknown[]) => request(...args),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  };
  return {
    ...actual,
    default: {
      ...actual.default,
      create: () => instance,
      isAxiosError: actual.default.isAxiosError,
    },
  };
});

const { AUTH_EXPIRED_EVENT, resetExpiredFlag, restoreSession, scheduleTokenRefresh, sessionExpired } = await import(
  "../services/http"
);

// 真实 JWT 的 base64url 段**不带填充**：这里刻意照现实构造，用来验证解码兼容逻辑
// （缺填充时 atob 在 Node/jsdom 会抛错 → 会导致「到期前续签」静默失效）。
const JWT_LIKE = (expSeconds: number) =>
  `header.${btoa(JSON.stringify({ exp: expSeconds })).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")}.sig`;

describe("http 会话层", () => {
  beforeEach(() => {
    post.mockReset();
    request.mockReset();
    resetExpiredFlag();
    useAuthStore.getState().clear();
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("restoreSession 在已有 token 时直接返回 true（不发请求）", async () => {
    useAuthStore.getState().setSession("token-1");
    await expect(restoreSession()).resolves.toBe(true);
    expect(post).not.toHaveBeenCalled();
  });

  it("restoreSession 用 refresh 换新 access 并写入 store", async () => {
    post.mockResolvedValue({ data: { data: { access_token: "fresh" } } });
    await expect(restoreSession()).resolves.toBe(true);
    expect(useAuthStore.getState().token).toBe("fresh");
    // 必须带 CSRF 头（backend 对 /auth/refresh 有此要求，缺了直接 403）
    const [, , config] = post.mock.calls[0];
    expect((config as { headers: Record<string, string> }).headers["X-Requested-With"]).toBe("fetch");
  });

  it("restoreSession 失败返回 false（不抛错，由调用方决定提示）", async () => {
    post.mockRejectedValue(new Error("401"));
    await expect(restoreSession()).resolves.toBe(false);
  });

  it("并发调用只发一次 refresh（单飞）", async () => {
    post.mockResolvedValue({ data: { data: { access_token: "fresh" } } });
    await Promise.all([restoreSession(), restoreSession(), restoreSession()]);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("sessionExpired 清空登录态并派发全局事件（只派发一次）", () => {
    useAuthStore.getState().setSession("token-1", { username: "u" });
    const handler = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, handler);
    sessionExpired();
    sessionExpired();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().token).toBeNull();
    window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
  });

  it("scheduleTokenRefresh 对畸形 token 直接返回（不排定时器、不抛错）", () => {
    useAuthStore.getState().setSession("not-a-jwt");
    expect(() => scheduleTokenRefresh()).not.toThrow();
  });

  it("scheduleTokenRefresh 对已过期 token 立即**真正**续签（force 分支，不能被「本地有 token」短路）", async () => {
    vi.useFakeTimers();
    post.mockResolvedValue({ data: { data: { access_token: "rotated" } } });
    useAuthStore.getState().setSession(JWT_LIKE(Math.floor(Date.now() / 1000) - 10));
    scheduleTokenRefresh();
    await vi.advanceTimersByTimeAsync(10);
    expect(post).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().token).toBe("rotated");
  });

  it("scheduleTokenRefresh 对「没有 exp」的 token 不排定时器（否则 delay=0 会变成续签风暴）", async () => {
    vi.useFakeTimers();
    post.mockResolvedValue({ data: { data: { access_token: "rotated" } } });
    // 合法 base64url、合法 JSON，但没有 exp 字段 —— 旧实现算成 delay=0 → 立刻续签 → 无限循环
    useAuthStore.getState().setSession("header.eyJ1c2VyIjoiYSJ9.sig");
    scheduleTokenRefresh();
    await vi.advanceTimersByTimeAsync(10);
    expect(post).not.toHaveBeenCalled();
  });

  it("到期前续签失败会重试一次；仍失败则弹「会话过期」（不把用户静默踢到登录页）", async () => {
    vi.useFakeTimers();
    post.mockRejectedValue(new Error("network"));
    useAuthStore.getState().setSession(JWT_LIKE(Math.floor(Date.now() / 1000) - 10));
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    scheduleTokenRefresh();
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(5000);
    expect(post).toHaveBeenCalledTimes(2); // 首次 + 5s 后重试
    expect(expired).toHaveBeenCalled();
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  });
});
