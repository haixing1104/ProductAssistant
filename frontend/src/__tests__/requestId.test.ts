// 请求关联 ID（`X-Request-Id`）用例：一次生成要跨 backend → Redis → ai-engine 三段日志，
// 这个 ID 是唯一能把三段串起来的东西（P8）。断言它「可用、唯一、能放进请求头」。
import { describe, expect, it, vi } from "vitest";

import { newRequestId } from "../services/http";

describe("newRequestId（X-Request-Id）", () => {
  it("非空且互不相同", () => {
    const first = newRequestId();
    const second = newRequestId();
    expect(first).toBeTruthy();
    expect(first).not.toBe(second);
  });

  it("可直接作为请求头值（无空白/控制字符）", () => {
    expect(newRequestId()).toMatch(/^[\x21-\x7E]+$/);
  });

  it("环境没有 crypto.randomUUID 时退化到 req- 前缀（老内核 / 非安全上下文）", () => {
    const original = globalThis.crypto;
    vi.stubGlobal("crypto", {} as Crypto);
    try {
      const id = newRequestId();
      expect(id.startsWith("req-")).toBe(true);
      expect(id.length).toBeGreaterThan(8);
    } finally {
      vi.stubGlobal("crypto", original);
    }
  });
});
