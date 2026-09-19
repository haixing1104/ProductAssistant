// 错误文案用例（**PA 信封口径**）：message 优先 → 429 读 Retry-After → CSV 逐行错误。
//
// 这条最容易照抄 PP 写错：PP 优先读 `detail`，而 PA 的可读原因在 `message`；
// 写错的表现是「所有业务错误都退化成状态码兜底文案」（如 SKU 冲突只显示「数据冲突」）。
import axios from "axios";
import { afterEach, describe, expect, it } from "vitest";

import { apiErrorMessage, csvRowErrors, isTransientGatewayFailure, retryAfterSeconds } from "../services/errors";
import { resetPlatform, setPlatform, type PaPlatform } from "../services/platform";

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

describe("网络类文案带目标基址（2026-09 真机「网络异常」排障口径）", () => {
  afterEach(() => {
    resetPlatform(); // 复位到 Web 默认实现，避免用例之间互相串平台
  });

  it("有绝对基址（RN）时把地址带进文案；Web 同源（基址为空）保持原文案", () => {
    // RN 平台实现返回绝对地址（模拟器默认 http://10.0.2.2:8000；真机是开发机局域网 IP）——
    // 地址填错时这句话就是最直接的线索（这次事故正是「看不出它打的是哪」）。
    setPlatform({ apiBaseUrl: () => "http://10.0.2.2:8000" } as unknown as PaPlatform);
    expect(apiErrorMessage(new axios.AxiosError("Network Error"))).toBe(
      "网络异常（http://10.0.2.2:8000），请检查网络后重试",
    );
    const timeout = new axios.AxiosError("timeout of 30000ms exceeded");
    timeout.code = "ECONNABORTED";
    expect(apiErrorMessage(timeout)).toBe("请求超时（http://10.0.2.2:8000），请稍后重试");

    // Web 的 apiBaseUrl() = ""（同源相对路径）→ 文案与改动前逐字一致（桌面端 / H5 零回归）
    resetPlatform();
    expect(apiErrorMessage(new axios.AxiosError("Network Error"))).toBe("网络异常，请检查网络后重试");
  });

  it("信封 message 优先于网络兜底（带地址也不能盖掉后端给的原因）", () => {
    setPlatform({ apiBaseUrl: () => "http://192.168.5.13:8000" } as unknown as PaPlatform);
    expect(apiErrorMessage(axiosError(409, { code: 409, data: null, message: "该组织下 SKU 已存在" }))).toBe(
      "该组织下 SKU 已存在",
    );
  });
});

// 2026-09 真机排障口径：**5xx 且响应体不是 PA 信封 ⇒ 挡在中间的是隧道/网关**。
//
// 现场：手机走隧道时审批偶发 503「依赖服务暂不可用，请稍后重试」，重试即成功 ——
// 而后端当日 0 个 5xx、审批请求在后端日志里**完全不存在**（请求没到后端）。
// 不区分来源的话，这句话会把人一路带向"后端依赖不可用"的错误结论（Redis/DB/OSS 全部白查）。
describe("网关/隧道 5xx 的来源提示与重试判定", () => {
  afterEach(() => {
    resetPlatform();
  });

  it("非信封 5xx 追加来源提示（530/502/503 这类中间层错误页）", () => {
    // 隧道边缘返回的是 HTML/纯文本（没有 message/detail）
    expect(apiErrorMessage(axiosError(503, "<html>503 Service Unavailable</html>"))).toBe(
      "依赖服务暂不可用，请稍后重试（非后端信封响应，可能来自隧道/网关）",
    );
    expect(apiErrorMessage(axiosError(502, ""))).toContain("（非后端信封响应，可能来自隧道/网关）");
    expect(apiErrorMessage(axiosError(500, undefined))).toContain("服务暂时开小差（500）");
  });

  it("后端自己的 5xx 是信封 → 原样展示 message，不加提示（避免误导）", () => {
    expect(apiErrorMessage(axiosError(500, { code: 500, data: null, message: "服务内部错误" }))).toBe("服务内部错误");
    // 未配置依赖的 503 也是信封（如 OSS 未配置）→ 保持后端原文，不加"隧道"字样
    expect(apiErrorMessage(axiosError(503, { code: 503, data: null, message: "OSS 未配置" }))).toBe("OSS 未配置");
    // 4xx 一律不加来源提示
    expect(apiErrorMessage(axiosError(404, "not found"))).toBe("内容不存在或已被删除");
  });

  it("isTransientGatewayFailure：只有「没到后端」的失败才算（幂等重试的判定口径）", () => {
    expect(isTransientGatewayFailure(new axios.AxiosError("Network Error"))).toBe(true); // 无响应
    expect(isTransientGatewayFailure(axiosError(503, "<html>edge</html>"))).toBe(true); // 非信封 5xx
    expect(isTransientGatewayFailure(axiosError(503, { code: 503, data: null, message: "OSS 未配置" }))).toBe(false);
    expect(isTransientGatewayFailure(axiosError(409, { code: 409, data: null, message: "已被他人定案" }))).toBe(false);
    expect(isTransientGatewayFailure(new Error("boom"))).toBe(false);
  });
});
