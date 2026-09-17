// 错误文案用例（**PA 信封口径**）：message 优先 → 429 读 Retry-After → CSV 逐行错误。
//
// 这条最容易照抄 PP 写错：PP 优先读 `detail`，而 PA 的可读原因在 `message`；
// 写错的表现是「所有业务错误都退化成状态码兜底文案」（如 SKU 冲突只显示「数据冲突」）。
import axios from "axios";
import { describe, expect, it } from "vitest";

import { apiErrorMessage, csvRowErrors, retryAfterSeconds } from "../services/errors";

/** 构造一个"像 axios 错误"的对象（含 response.data / response.headers）。 */
function axiosError(status: number, body: unknown, headers: Record<string, string> = {}) {
  const error = new axios.AxiosError("Request failed");
  // @ts-expect-error 测试替身：只填被测代码会读到的字段
  error.response = { status, data: body, headers };
  return error;
}

describe("apiErrorMessage", () => {
  it("优先展示信封 message（PA 的业务原因）", () => {
    const e = axiosError(409, { code: 409, data: null, message: "该组织下 SKU 已存在" });
    expect(apiErrorMessage(e)).toBe("该组织下 SKU 已存在");
  });

  it("429 带 Retry-After → 把「还要等多久」告诉用户", () => {
    const e = axiosError(429, { code: 429, data: null, message: "登录失败次数过多，请稍后再试" }, { "retry-after": "300" });
    expect(retryAfterSeconds(e)).toBe(300);
    // message 优先（后端已给文案），但仍能取到秒数供 UI 展示
    expect(apiErrorMessage(e)).toContain("登录失败次数过多");
  });

  it("没有 message 时按状态码兜底；405 特判为「已下线」提示（软删端点）", () => {
    expect(apiErrorMessage(axiosError(405, { code: 405, data: null }))).toContain("已下线");
    expect(apiErrorMessage(axiosError(404, { code: 404, data: null }))).toBe("内容不存在或已被删除");
    expect(apiErrorMessage(axiosError(503, { code: 503, data: null }))).toContain("依赖服务");
  });

  it("无响应（网络异常）与超时分别给可读文案", () => {
    const network = new axios.AxiosError("Network Error");
    expect(apiErrorMessage(network)).toBe("网络异常，请检查网络后重试");
    const timeout = new axios.AxiosError("timeout of 30000ms exceeded");
    timeout.code = "ECONNABORTED";
    expect(apiErrorMessage(timeout)).toBe("请求超时，请稍后重试");
  });

  it("非 axios 异常回退到 fallback 或 Error.message", () => {
    expect(apiErrorMessage(new Error("boom"), "兜底")).toBe("boom");
    expect(apiErrorMessage("plain", "兜底")).toBe("兜底");
  });
});

describe("csvRowErrors", () => {
  it("从 data.row_errors 取逐行原因（PA 把行错误放在 data 里，不是 message）", () => {
    const e = axiosError(400, {
      code: 400,
      message: "CSV 校验失败",
      data: { row_errors: [{ line: 3, sku_code: "SKU-3", reason: "base_price 非法" }] },
    });
    expect(csvRowErrors(e)).toEqual([{ line: 3, sku_code: "SKU-3", reason: "base_price 非法" }]);
  });

  it("无 row_errors 时返回空数组（不抛错）", () => {
    expect(csvRowErrors(axiosError(400, { code: 400, data: null }))).toEqual([]);
    expect(csvRowErrors(new Error("x"))).toEqual([]);
  });
});

describe("retryAfterSeconds", () => {
  it("缺失或非法 Retry-After 返回 null", () => {
    expect(retryAfterSeconds(axiosError(429, {}, {}))).toBeNull();
    expect(retryAfterSeconds(axiosError(429, {}, { "retry-after": "not-a-number" }))).toBeNull();
    expect(retryAfterSeconds(new Error("x"))).toBeNull();
  });
});
