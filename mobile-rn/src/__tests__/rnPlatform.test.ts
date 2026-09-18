// RN 平台实现用例 —— 钉住"手脚"层最容易错、且错了**静默**的四件事：
//   ① JWT exp 解码（错 → 到期前续签静默失效，用户感觉"用着用着被踢"）；
//   ② SSE 传输（错 → 页面永远转圈，或把空闲当故障）；
//   ③ 上传（错 → OSS 直传 403，图片永远传不上去）；
//   ④ 平台注册（装晚了 → 走 Web 默认实现，相对路径在原生端直接发不出去）。
//
// 这些用例**不需要原生运行时**：expo 模块被 mock，共享层的读流/帧语义用真实的 TextDecoder 跑，
// 因此这里验证的是"共享语义在 RN 传输下是否仍然成立"，而不是复述 mock 的调用参数。
import { getPlatform, resetPlatform, type PaUploadFile } from "@pa/core/services/platform";

import { decodeBase64UrlToString, utf8Decode } from "../platform/base64";
import { resolveApiBaseUrl, resolveDesktopBaseUrl } from "../platform/env";

jest.mock("expo-crypto", () => ({ randomUUID: jest.fn(() => "uuid-fixed-1") }));
jest.mock("expo/fetch", () => ({ fetch: jest.fn() }));
jest.mock("expo-file-system/legacy", () => ({
  uploadAsync: jest.fn(),
  FileSystemUploadType: { BINARY_CONTENT: 0, MULTIPART: 1 },
}));

const expoFetch = require("expo/fetch").fetch as jest.Mock;
const FileSystem = require("expo-file-system/legacy") as {
  uploadAsync: jest.Mock;
  FileSystemUploadType: { BINARY_CONTENT: number };
};
const { RN_PLATFORM, installRnPlatform } = require("../platform/rnPlatform") as typeof import("../platform/rnPlatform");

const encode = (text: string): Uint8Array => new TextEncoder().encode(text);

/** 造一个「可流式读」的假 Response：按帧喂数据，读完即 done。 */
function streamingResponse(chunks: string[], init: { status?: number; ok?: boolean } = {}) {
  const status = init.status ?? 200;
  let index = 0;
  return {
    ok: init.ok ?? status < 400,
    status,
    body: {
      getReader: () => ({
        read: async () => {
          if (index >= chunks.length) return { done: true, value: undefined };
          const value = encode(chunks[index]);
          index += 1;
          return { done: false, value };
        },
        cancel: async () => undefined,
      }),
    },
  };
}

describe("base64url 解码（JWT exp 的依赖）", () => {
  it("解出无填充的 base64url 载荷（含中文也不乱码）", () => {
    const payload = JSON.stringify({ exp: 1700000000, username: "张伟" });
    const base64url = Buffer.from(payload, "utf8").toString("base64url"); // 不带 `=` 填充
    expect(decodeBase64UrlToString(base64url)).toBe(payload);
  });

  it("非法字符返回 null（不抛错）", () => {
    expect(decodeBase64UrlToString("不是 base64!!")).toBeNull();
  });

  it("手写 UTF-8 解码：多字节正常、非法序列不抛错", () => {
    expect(utf8Decode(encode("商品标题"))).toBe("商品标题");
    expect(utf8Decode(Uint8Array.from([0xff, 0xfe]))).toContain("\uFFFD");
  });
});

describe("地址解析（RN 没有 dev 代理）", () => {
  it("未配置时按平台给可用的联调默认值", () => {
    expect(resolveApiBaseUrl({}, "android")).toBe("http://10.0.2.2:8000");
    expect(resolveApiBaseUrl({}, "ios")).toBe("http://localhost:8000");
  });

  it("配置了就用配置值，并去掉末尾斜杠（共享层会自己拼 /api/v1）", () => {
    expect(resolveApiBaseUrl({ EXPO_PUBLIC_API_BASE_URL: "http://192.168.1.5:8000/" }, "android")).toBe(
      "http://192.168.1.5:8000",
    );
  });

  it("桌面端地址默认 :5173（「我的」页的电脑端入口）", () => {
    expect(resolveDesktopBaseUrl({})).toBe("http://localhost:5173");
    expect(resolveDesktopBaseUrl({ EXPO_PUBLIC_DESKTOP_BASE_URL: "https://pa.example.com/" })).toBe(
      "https://pa.example.com",
    );
  });
});

describe("平台注册表", () => {
  afterEach(() => {
    resetPlatform();
  });

  it("默认是 Web 实现；installRnPlatform 之后变成 RN（且幂等）", () => {
    expect(getPlatform().name).toBe("web");
    installRnPlatform();
    expect(getPlatform().name).toBe("rn");
    installRnPlatform();
    expect(getPlatform().name).toBe("rn");
  });
});

describe("RN 平台实现", () => {
  beforeEach(() => {
    expoFetch.mockReset();
    FileSystem.uploadAsync.mockReset();
    installRnPlatform();
  });

  it("接口基址是绝对地址（原生端没有同源代理，相对路径发不出去）", () => {
    expect(RN_PLATFORM.apiBaseUrl()).toMatch(/^https?:\/\//);
  });

  it("请求关联 ID 用 expo-crypto（无空白字符，可直接作请求头）", () => {
    expect(RN_PLATFORM.newRequestId()).toBe("uuid-fixed-1");
    expect(RN_PLATFORM.newRequestId()).toMatch(/^\S+$/);
  });

  it("会话回跳：登录页不记；其它页记下后取一次即清空（与 sessionStorage 语义一致）", () => {
    RN_PLATFORM.setCurrentPath("/login");
    RN_PLATFORM.rememberReturnUrl();
    expect(RN_PLATFORM.takeReturnUrl()).toBeNull();

    RN_PLATFORM.setCurrentPath("/approvals/a-1");
    RN_PLATFORM.rememberReturnUrl();
    expect(RN_PLATFORM.takeReturnUrl()).toBe("/approvals/a-1");
    expect(RN_PLATFORM.takeReturnUrl()).toBeNull();
  });

  it("会话过期：可多订阅、可退订；单个订阅者抛错不影响其它订阅者", () => {
    const first = jest.fn();
    const second = jest.fn();
    const unsubscribe = RN_PLATFORM.onAuthExpired(first);
    RN_PLATFORM.onAuthExpired(() => {
      throw new Error("订阅者自己炸了");
    });
    RN_PLATFORM.onAuthExpired(second);

    RN_PLATFORM.emitAuthExpired();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);

    unsubscribe();
    RN_PLATFORM.emitAuthExpired();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(2);
  });

  it("JWT exp：真 token 能解出毫秒；畸形/无 exp 返回 null（不排续签定时器）", () => {
    const token = (payload: Record<string, unknown>) =>
      `header.${Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")}.sig`;
    expect(RN_PLATFORM.jwtExpMs(token({ exp: 1700000000 }))).toBe(1700000000000);
    expect(RN_PLATFORM.jwtExpMs(token({}))).toBeNull();
    expect(RN_PLATFORM.jwtExpMs("not-a-jwt")).toBeNull();
  });

  describe("openSse（传输走 expo/fetch，帧语义由共享层判定）", () => {
    const request = (headers: Record<string, string> = {}) => ({
      url: "http://x/api/v1/products/p-1/stream?ticket=t",
      headers,
      signal: new AbortController().signal,
    });

    it("hitl.waiting 是终态：立刻停，不再等 approval.resumed", async () => {
      expoFetch.mockResolvedValue(streamingResponse(['data: {"type":"hitl.waiting","data":{}}\n\n']));
      const frames: { type?: string }[] = [];
      const attempt = await RN_PLATFORM.openSse(request(), (frame) => frames.push(frame));
      expect(attempt.outcome).toBe("terminal");
      expect(frames[0].type).toBe("hitl.waiting");
    });

    it("ready 控制帧 → 停（不空转重连）", async () => {
      expoFetch.mockResolvedValue(streamingResponse(['data: {"type":"ready","data":{}}\n\n']));
      const attempt = await RN_PLATFORM.openSse(request(), () => undefined);
      expect(attempt.outcome).toBe("ready");
    });

    it("注释帧 stream-idle-close → idle（可续连），且注释帧会交给回调", async () => {
      expoFetch.mockResolvedValue(streamingResponse([": stream-idle-close (last_id=1-0)\n\n"]));
      const frames: { comment?: string }[] = [];
      const attempt = await RN_PLATFORM.openSse(request(), (frame) => frames.push(frame));
      expect(attempt.outcome).toBe("idle");
      expect(frames[0].comment).toContain("stream-idle-close");
    });

    it("普通事件读完（对端优雅关流）→ eof，且 Last-Event-ID 随请求头带出", async () => {
      expoFetch.mockResolvedValue(streamingResponse(['data: {"type":"content.chunk","data":{"text":"你好"}}\n\n']));
      const attempt = await RN_PLATFORM.openSse(
        request({ Accept: "text/event-stream", "Last-Event-ID": "1-0" }),
        () => undefined,
      );
      expect(attempt.outcome).toBe("eof");
      expect(expoFetch.mock.calls[0][1].headers["Last-Event-ID"]).toBe("1-0");
    });

    it("HTTP 非 2xx → error + 可读原因（进 UI 文案与重试判定）", async () => {
      expoFetch.mockResolvedValue(streamingResponse([], { status: 403, ok: false }));
      const attempt = await RN_PLATFORM.openSse(request(), () => undefined);
      expect(attempt).toEqual({ outcome: "error", failure: "HTTP 403" });
    });

    it("响应体不可读时明确报错（否则页面会停在等待事件、永不报错）", async () => {
      expoFetch.mockResolvedValue({ ok: true, status: 200, body: null });
      const attempt = await RN_PLATFORM.openSse(request(), () => undefined);
      expect(attempt.outcome).toBe("error");
      expect(attempt.failure).toContain("响应体不可读");
    });

    it("abort 不算失败（调用方据 signal.aborted 退出，不会误报连接中断）", async () => {
      const ctrl = new AbortController();
      expoFetch.mockImplementation(async () => {
        ctrl.abort();
        throw new Error("Aborted");
      });
      const attempt = await RN_PLATFORM.openSse(
        { url: "http://x/stream", headers: {}, signal: ctrl.signal },
        () => undefined,
      );
      expect(attempt.outcome).toBe("eof");
    });
  });

  describe("putBinary（OSS 预签名直传要的是裸字节体）", () => {
    const file: PaUploadFile = { uri: "file:///tmp/a.jpg", name: "a.jpg", type: "image/jpeg" };

    it("用 PUT + BINARY_CONTENT + 同一个 Content-Type（它参与签名）", async () => {
      FileSystem.uploadAsync.mockResolvedValue({ status: 200, headers: {}, body: "" });
      await RN_PLATFORM.putBinary("https://oss.example.com/put?sig=1", "image/jpeg", file);
      const [url, uri, options] = FileSystem.uploadAsync.mock.calls[0];
      expect(url).toBe("https://oss.example.com/put?sig=1");
      expect(uri).toBe("file:///tmp/a.jpg");
      expect(options.httpMethod).toBe("PUT");
      expect(options.uploadType).toBe(FileSystem.FileSystemUploadType.BINARY_CONTENT);
      expect(options.headers["Content-Type"]).toBe("image/jpeg");
    });

    it("非 2xx 抛错（页面据此提示失败，而不是假装成功）", async () => {
      FileSystem.uploadAsync.mockResolvedValue({ status: 403, headers: {}, body: "denied" });
      await expect(RN_PLATFORM.putBinary("https://oss/put", "image/jpeg", file)).rejects.toThrow("HTTP 403");
    });

    it("缺少本地路径（例如误传 Web 的 File）立即失败，不发起上传", async () => {
      await expect(
        RN_PLATFORM.putBinary("https://oss/put", "image/jpeg", {} as unknown as PaUploadFile),
      ).rejects.toThrow("缺少本地路径");
      expect(FileSystem.uploadAsync).not.toHaveBeenCalled();
    });
  });
});

