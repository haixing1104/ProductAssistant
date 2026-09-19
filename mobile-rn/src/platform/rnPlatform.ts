// RN 平台实现（平台端口的"手脚"）—— 把共享契约层的 6 个平台差异点接上原生能力。
//
// 设计要点（改动前先读）:
//   1) **不复制任何危险逻辑**：`connectProductStream` 的重连/终态判断、`http.ts` 的单飞续签与
//      401 重放、`parseSseFrame`/`frameOutcome` 的帧语义**全部在共享层**；这里只提供
//      「存储 / 事件 / 定时器 / 随机 ID / JWT 解码 / 传输」六件事。
//   2) 读流复用共享的 `consumeSse`（浏览器那份）：RN 侧 Metro 注入 `ReadableStream`，
//      expo 的 winter runtime 全局安装 `TextDecoder`（见 expo/src/winter/runtime.native.ts），
//      所以同一份读流代码在原生端也能跑 —— 这正是"共享而非复制"的关键收益。
//   3) 上传走 `expo-file-system/legacy` 的 `BINARY_CONTENT` PUT：RN 没有 `File`/`Blob` 直传能力，
//      而 OSS 预签名 PUT 要的是**裸字节体**（不是 multipart），这是两端最容易踩空的一处。
import * as Crypto from "expo-crypto";
import * as FileSystem from "expo-file-system/legacy";
import { fetch as expoFetch } from "expo/fetch";

import {
  consumeSse,
  setPlatform,
  type PaPlatform,
  type PaSseAttempt,
  type PaSseRequest,
  type PaUploadFile,
} from "@pa/core/services/platform";
import type { SseFrame } from "@pa/core/services/sse";

import { decodeBase64UrlToString } from "./base64";
import { resolveApiBaseUrl } from "./env";

// ---------- 会话回跳（与 H5 的 sessionStorage 语义等价：仅本会话内有效，不做跨进程持久化）----------
// 说明：sessionStorage 本来就是"标签页会话内"的存储，RN 用内存变量是最贴近的对应物；
// 冷启动后回跳地址丢失是可接受的（H5 关掉标签页也一样）。
let currentPath: string | null = null;
let returnUrl: string | null = null;

// ---------- 会话过期事件（Web 是 window 事件；RN 用内部 emitter）----------
const authExpiredHandlers = new Set<() => void>();

/** 兜底关联 ID（expo-crypto 不可用时；与 Web 端口同格式，便于日志统一检索）。 */
function fallbackRequestId(): string {
  return `req-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
}

/** RN 平台实现（`installRnPlatform()` 装到共享层的注册表上）。 */
export const RN_PLATFORM: PaPlatform = {
  name: "rn",

  apiBaseUrl: () => resolveApiBaseUrl(),

  setCurrentPath: (path) => {
    currentPath = path;
  },

  rememberReturnUrl: () => {
    // 与 Web 同口径：登录页本身不记（否则会话过期后会"回跳"到登录页，等于没回跳）
    if (currentPath && !currentPath.startsWith("/login")) returnUrl = currentPath;
  },

  takeReturnUrl: () => {
    const value = returnUrl;
    returnUrl = null;
    return value;
  },

  emitAuthExpired: () => {
    // 复制一份再遍历：回调里可能会退订（React 卸载）
    [...authExpiredHandlers].forEach((handler) => {
      try {
        handler();
      } catch {
        // 单个订阅者出错不影响其它订阅者（会话过期提示必须"尽力送达"）
      }
    });
  },

  onAuthExpired: (handler) => {
    authExpiredHandlers.add(handler);
    return () => authExpiredHandlers.delete(handler);
  },

  setTimeout: (fn, ms) => {
    const timer = setTimeout(fn, ms);
    return () => clearTimeout(timer);
  },

  newRequestId: () => {
    try {
      return Crypto.randomUUID();
    } catch {
      // 极端环境（加密模块不可用）退化为随机串：ID 只要能"唯一标识一次请求"就够
      return fallbackRequestId();
    }
  },

  jwtExpMs: (token) => {
    const part = token.split(".")[1];
    if (!part) return null;
    const json = decodeBase64UrlToString(part);
    if (json === null) return null;
    try {
      const exp = (JSON.parse(json) as { exp?: unknown }).exp;
      // 只有"正数"的 exp 才排续签定时器：缺失/非法时返回 null，
      // 避免算出 delay=0 → 立刻续签 → 又排一次 → **续签风暴**（Web 侧同口径，见 platform.ts）。
      return typeof exp === "number" && Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
    } catch {
      return null;
    }
  },

  openSse: async ({ url, headers, signal }: PaSseRequest, onFrame: (frame: SseFrame) => void): Promise<PaSseAttempt> => {
    try {
      // expo/fetch 支持**真正的流式响应体**（`response.body.getReader()`），
      // 因此下面直接复用共享层那份读流实现（含注释帧三态），不需要为 RN 再写一套。
      const response = await expoFetch(url, { headers, signal });
      if (!response.ok) return { outcome: "error", failure: `HTTP ${response.status}` };
      if (!response.body) {
        // 显式失败而不是"静默空流"：否则页面会停在「暂无事件（等待服务端推送…）」且永远不报错
        return { outcome: "error", failure: "响应体不可读（需要 expo/fetch 的流式支持）" };
      }
      return { outcome: await consumeSse(response as unknown as Response, onFrame, signal) };
    } catch (e) {
      // abort 不算失败：调用方紧接着会用 signal.aborted 判断并退出循环（与 Web 实现同口径）
      if (signal.aborted) return { outcome: "eof" };
      return { outcome: "error", failure: String(e) };
    }
  },

  putBinary: async (url: string, contentType: string, file: PaUploadFile): Promise<void> => {
    const uri = (file as { uri?: string }).uri;
    if (!uri) throw new Error("上传文件缺少本地路径：RN 需要 {uri, name, type}");
    const result = await FileSystem.uploadAsync(url, uri, {
      httpMethod: "PUT",
      // BINARY_CONTENT = 把文件当**裸请求体**发送（不是 multipart）—— OSS 预签名 PUT 要的就是这个
      uploadType: FileSystem.FileSystemUploadType.BINARY_CONTENT,
      // Content-Type 必须与 presign 时签名的一致，否则 OSS 直接拒绝
      headers: { "Content-Type": contentType },
    });
    if (result.status < 200 || result.status >= 300) {
      // **必须把 OSS 的响应体带上**：403 是 SignatureDoesNotMatch / AccessDenied，
      // 404 是 NoSuchBucket，而 503 SlowDown 是限流 —— 只报状态码时"图传不上去"
      // 永远无法定位（2026-09 真机实测：presign 200 之后 PUT 静默失败、没有任何线索）。
      const detail = (result.body ?? "").replace(/\s+/g, " ").trim().slice(0, 200);
      throw new Error(`OSS 直传失败 HTTP ${result.status}${detail ? `：${detail}` : ""}`);
    }
  },
};

let installed = false;

/**
 * 把 RN 平台实现装到共享层的注册表上（**必须在任何请求/流连接之前调用**）。
 *
 * 幂等：重复调用无副作用。放在 `index.ts`（入口）而不是 App 里，是为了保证
 * "平台已就绪"不依赖组件树的渲染顺序。
 */
export function installRnPlatform(): void {
  if (installed) return;
  installed = true;
  setPlatform(RN_PLATFORM);
}

