// 平台端口（**共享契约层的唯一平台差异出口**）。
//
// 为什么要有这一层（而不是让 RN 侧复制 `http.ts` / `sse.ts`）:
//   这两块是本仓历史上最容易流血的逻辑 —— `http.ts`（内存 access + 单飞续签 + 401 重放 + 流票据）
//   与 `sse.ts`（`hitl.waiting` 是终态 / `ready` 不重连 / 注释帧三态 / `Last-Event-ID` 续传）。
//   复制一份 = 以后每次修复都要改两处。而它们与浏览器的耦合其实只有 6 个点：
//     ① 接口基址（Web 同源相对路径 vs RN 绝对地址）
//     ② 会话回跳地址（sessionStorage / window.location）
//     ③ 会话过期通知（window.dispatchEvent）
//     ④ 定时器（window.setTimeout）
//     ⑤ 请求关联 ID（crypto.randomUUID）与 JWT exp 解码（atob）
//     ⑥ 传输：SSE 读流（fetch + body.getReader + TextDecoder）/ 二进制直传（File + fetch PUT）
//   抽成端口后：**重连/终态/续签语义全部留在共享层**，两端只换"手脚"，不换"大脑"。
//
// 默认实现 = **Web**（逐行搬移自旧 http.ts / sse.ts，行为零变化）：
//   桌面端与 mobile-h5 不调用 setPlatform，直接走默认分支；RN 在入口 index.ts 覆盖。
//
// ⚠️ 本文件所有 DOM 调用都在**函数体内**（模块顶层零副作用），
//    因此即使 RN 的 bundle 里包含这份文件也不会在加载时崩（只是永远不会被调用）。
//
// ⚠️ 与 `sse.ts` 存在**循环引用**（sse.ts → getPlatform；platform.ts → parseSseFrame）。
//    这是安全的：双方都只在**函数体内**使用对方的绑定，且顶层只做定义、不做调用。
import { frameOutcome, parseSseFrame, type SseFrame } from "./sse";

/** 一次 SSE 连接的走向（与 backend 的流生命周期一一对应；调用方据此决定是否重连）。 */
export type PaSseOutcome = "terminal" | "ready" | "idle" | "error" | "eof";

export interface PaSseRequest {
  /** 已含票据的完整地址（Web 是相对路径；RN 是绝对地址） */
  url: string;
  headers: Record<string, string>;
  /** 断线续传：服务端据此从上次位置继续推 */
  lastEventId?: string;
  signal: AbortSignal;
}

export interface PaSseAttempt {
  outcome: PaSseOutcome;
  /** `outcome === "error"` 时的可读原因（进 UI 文案，例如 `HTTP 403`） */
  failure?: string;
}

/**
 * 可上传文件：
 *   · Web = 原生 `File`（`<input type=file>` 产物）；
 *   · RN  = `{uri, name, type}`（expo-document-picker / expo-image-picker 的产物）。
 */
export type PaUploadFile = File | { uri: string; name: string; type: string };

export interface PaPlatform {
  readonly name: "web" | "rn";

  /** 接口基址。Web = ""（同源相对路径：dev 由 vite 代理、生产由 nginx 反代）；RN = 绝对地址。 */
  apiBaseUrl(): string;

  /** 记录"当前所在路由"（Web 从 window.location 推导；RN 由导航层喂进来）。 */
  setCurrentPath(path: string): void;
  /** 记录"从哪来"，供会话过期后重新登录时回跳。 */
  rememberReturnUrl(): void;
  /** 取走并清空回跳地址（各端 Login 页共用）。 */
  takeReturnUrl(): string | null;

  /** 会话真实失效：通知 UI 弹「登录已过期」（Web = window 事件；RN = 内部 emitter）。 */
  emitAuthExpired(): void;
  /** 订阅会话失效（返回取消订阅函数）。 */
  onAuthExpired(handler: () => void): () => void;

  /** 一次性定时器，返回取消函数。 */
  setTimeout(fn: () => void, ms: number): () => void;

  /** 请求关联 ID（`X-Request-Id`）：把 backend → Redis → ai-engine 三段日志串起来。 */
  newRequestId(): string;

  /** 解 JWT 的 `exp`（毫秒）；**解析失败或 exp 非正数返回 null**（调用方据此不排续签定时器）。 */
  jwtExpMs(token: string): number | null;

  /** 打开一次 SSE 连接并读到结束/终态（Web = fetch + reader；RN = expo/fetch）。 */
  openSse(req: PaSseRequest, onFrame: (frame: SseFrame) => void): Promise<PaSseAttempt>;

  /** 二进制直传（OSS 预签名 PUT：**Content-Type 必须与签名时一致**）。 */
  putBinary(url: string, contentType: string, file: PaUploadFile): Promise<void>;
}

/** 会话过期事件名（与桌面端 `SessionExpiredGate`、H5 `SessionGate` 的监听口径一致）。 */
export const AUTH_EXPIRED_EVENT = "pa:auth-expired";

/** 回跳地址的存储键（Web 用 sessionStorage；RN 用 AsyncStorage，键名一致便于排查）。 */
export const RETURN_URL_KEY = "pa_return";

let current: PaPlatform | null = null;

/** 覆盖平台实现（RN 在入口调用）。Web 端**不需要**调用 —— 默认即 Web。 */
export function setPlatform(platform: PaPlatform): void {
  current = platform;
}

/** 复位到默认（Web）实现：仅供测试使用，避免用例之间互相串平台。 */
export function resetPlatform(): void {
  current = null;
}

export function getPlatform(): PaPlatform {
  return current ?? WEB_PLATFORM;
}

/** 拼接口绝对/相对地址（Web 拼出来仍是相对路径，与改动前完全一致）。 */
export function apiUrl(path: string): string {
  return `${getPlatform().apiBaseUrl()}${path}`;
}


// ============================== Web 默认实现 ==============================

/** 安全取 sessionStorage（隐私模式/禁用时返回 null，调用方退化为"没记住"）。 */
function safeSessionStorage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

/**
 * 逐帧读取 SSE 响应体，直到结束/终态（**浏览器专属**：依赖 `body.getReader()`）。
 *
 * 参数:
 *   response: 已建立的 SSE 响应（`resp.body` 必须非空）。
 *   onFrame: 每帧回调（**含注释帧**，供 UI 区分「空闲关流」与「读流异常」）。
 *   signal: 中断信号（组件卸载/切换商品时 abort）。
 * 返回:
 *   `terminal` / `ready` / `idle` / `error` / `eof`（语义见 `PaSseOutcome`）。
 */
export async function consumeSse(
  response: Response,
  onFrame: (frame: SseFrame) => void,
  signal: AbortSignal,
): Promise<PaSseOutcome> {
  if (!response.body) return "eof";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      if (signal.aborted) return "eof";
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const frame = parseSseFrame(chunk.split("\n"));
        if (!frame) continue;
        onFrame(frame);
        const outcome = frameOutcome(frame);
        if (outcome) return outcome;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // 已取消/已关闭：无需处理
    }
  }
  return "eof";
}

/** Web 端实现（= 旧 `http.ts` / `sse.ts` 里的平台相关代码，行为逐行保持）。 */
const WEB_PLATFORM: PaPlatform = {
  name: "web",

  apiBaseUrl: () => "",

  setCurrentPath: () => {
    // Web 自己能从 window.location 推导，无需外部喂
  },

  rememberReturnUrl: () => {
    try {
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        safeSessionStorage()?.setItem(
          RETURN_URL_KEY,
          window.location.pathname + window.location.search,
        );
      }
    } catch {
      // sessionStorage 不可用（隐私模式等）时忽略
    }
  },

  takeReturnUrl: () => {
    const storage = safeSessionStorage();
    try {
      const url = storage?.getItem(RETURN_URL_KEY) ?? null;
      storage?.removeItem(RETURN_URL_KEY);
      return url;
    } catch {
      return null;
    }
  },

  emitAuthExpired: () => {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  },

  onAuthExpired: (handler) => {
    if (typeof window === "undefined") return () => undefined;
    const listener = () => handler();
    window.addEventListener(AUTH_EXPIRED_EVENT, listener);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
  },

  setTimeout: (fn, ms) => {
    // 用 globalThis 而非 window：浏览器/jsdom 等价，且在非 DOM 环境（SSR/脚本）也不会抛
    const timer = globalThis.setTimeout(fn, ms);
    return () => globalThis.clearTimeout(timer);
  },

  newRequestId: () => {
    const cryptoObj = globalThis.crypto as Crypto | undefined;
    if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
    return `req-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
  },

  jwtExpMs: (token) => {
    try {
      const part = token.split(".")[1];
      // JWT 的 base64url **不带填充**，而 atob 在部分运行时（Node/jsdom）要求长度对齐 4 ——
      // 不补 `=` 会直接抛 InvalidCharacterError，导致「到期前续签」静默失效（只剩 401 兜底）。
      const base64 = part.replace(/-/g, "+").replace(/_/g, "/");
      const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
      const json = decodeURIComponent(escape(atob(padded)));
      const exp = (JSON.parse(json) as { exp?: unknown }).exp;
      // 与 RN 实现同口径：只有「正数 exp」才排得出续签时刻；否则返回 null（调用方不排定时器）。
      // 旧实现返回 `(exp ?? 0) * 1000`：没有 exp 的 token 会算出 delay=0 → 立刻续签 → 又排一次，
      // 变成**续签风暴**（本地开发拿手搓 token 联调时最容易撞上）。
      return typeof exp === "number" && Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
    } catch {
      return null;
    }
  },

  openSse: async ({ url, headers, signal }, onFrame) => {
    try {
      const resp = await fetch(url, { headers, signal });
      if (!resp.ok) return { outcome: "error", failure: `HTTP ${resp.status}` };
      return { outcome: await consumeSse(resp, onFrame, signal) };
    } catch (e) {
      // abort 不算失败：调用方紧接着会用 signal.aborted 判断并退出循环
      if (signal.aborted) return { outcome: "eof" };
      return { outcome: "error", failure: String(e) };
    }
  },

  putBinary: async (url, contentType, file) => {
    const resp = await fetch(url, {
      method: "PUT",
      // PUT 必须带**同一个** Content-Type（预签名时签的就是它，不一致会被 OSS 拒绝）
      headers: { "Content-Type": contentType },
      body: file as File,
    });
    if (!resp.ok) throw new Error(`OSS 直传失败 HTTP ${resp.status}`);
  },
};

