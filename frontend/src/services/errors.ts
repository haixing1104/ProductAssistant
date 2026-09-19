// 统一错误提示文案（**PA 信封口径**，与 ProductPilot 的关键差异见下）。
//
// PA backend 的所有错误都是 `{code, data, message}`（`core/errors.py` 单点注册），
// 可读原因在 **`message`** 字段；而 PP 的旧口径把原因放在 `detail`。
// 若照抄 PP 的「优先读 detail」实现，PA 前端会**静默退化成状态码兜底文案**：
// 「SKU 已存在」「该词已存在」这类真正有用的原因会全部丢失（实测过）。
//
// 另外两类 PA 专属细节：
//   · CSV 导入的逐行错误在 **`data.row_errors`**（响应体 data 里，不是 message）；
//   · 登录限流 429 带 `Retry-After` 响应头 —— 必须把「还要等多久」告诉用户，
//     否则用户只知道「被限流了」却不知道该等 30 秒还是 15 分钟；
//   · 网络类文案带上**目标基址**（仅当平台基址非空时，见 `targetSuffix`）—— 2026-09 实测：
//     真机「登录报网络异常」的排查成本几乎全花在「不知道它打的是哪个地址」上。
import axios from "axios";

import { getPlatform } from "./platform";

interface EnvelopeBody {
  code?: number;
  data?: unknown;
  message?: string;
  // 兼容 PP 风格（防御性：万一被中间层改写）
  detail?: string;
}

/** CSV 导入的逐行错误（来自 `400 + data.row_errors`；导出给页面渲染错误表格）。 */
export interface CsvRowError {
  line: number;
  sku_code?: string;
  reason: string;
}

/** 从 429 响应里取「还需等待秒数」（无该头则 null）。 */
export function retryAfterSeconds(e: unknown): number | null {
  if (!axios.isAxiosError(e)) return null;
  const raw = (e.response?.headers as Record<string, unknown> | undefined)?.["retry-after"];
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : null;
}

/** 取后端返回的逐行错误（CSV 导入用；无则空数组）。 */
export function csvRowErrors(e: unknown): CsvRowError[] {
  if (!axios.isAxiosError(e)) return [];
  const body = e.response?.data as { data?: { row_errors?: unknown } } | undefined;
  const rows = body?.data?.row_errors;
  return Array.isArray(rows) ? (rows as CsvRowError[]) : [];
}

/**
 * 目标基址后缀：把「这次请求打的是哪个后端」写进网络类文案（排障用）。
 *
 * 为什么只在该端**有绝对基址**时附加：
 *   · Web（桌面端 / H5）的 `apiBaseUrl()` 是 `""` —— 同源相对路径（dev 走 vite 代理、生产走
 *     nginx 反代），「地址」对用户没有意义，文案保持原样（三端既有用例零回归）；
 *   · RN 的 `apiBaseUrl()` 是绝对地址（模拟器默认 `10.0.2.2` / 真机是开发机局域网 IP）——
 *     填错时界面直接把错误地址显示出来，不必再去翻后端日志比对时间线。
 * 取平台端口而不是读 `EXPO_PUBLIC_*`：平台差异一律走 `services/platform.ts`（模块红线）。
 */
function targetSuffix(): string {
  const base = getPlatform().apiBaseUrl();
  return base ? `（${base}）` : "";
}

/**
 * 响应体是否是 PA 信封（`message` / `detail` 里至少有一个非空字符串）。
 *
 * 为什么要判它：backend 的**所有**错误都是信封（`core/errors.py` 单点注册，
 * 连未捕获异常也落 500 信封）—— 所以"5xx 但没有信封"必然是**中间层**生成的
 * （隧道/反向代理/CDN 的错误页，通常是 HTML 或纯文本）。
 */
function isEnvelope(body: EnvelopeBody | undefined): boolean {
  const message = body?.message;
  if (typeof message === "string" && message.trim()) return true;
  const detail = body?.detail;
  return typeof detail === "string" && detail.trim().length > 0;
}

/** 状态码兜底文案（信封 message 之后的第二优先级）。 */
function statusMessage(status: number, e: unknown): string {
  switch (status) {
    case 400:
      return "请求有误，请检查填写内容后重试";
    case 401:
      return "登录已过期，请重新登录";
    case 403:
      return "没有权限执行该操作";
    case 404:
      return "内容不存在或已被删除";
    case 405:
      return "该操作已下线，请使用「彻底删除」";
    case 409:
      return "数据冲突，请刷新后重试";
    case 429: {
      const wait = retryAfterSeconds(e);
      return wait ? `操作过于频繁，请 ${wait} 秒后再试` : "操作过于频繁，请稍后再试";
    }
    case 503:
      return "依赖服务暂不可用，请稍后重试";
    default:
      return `服务暂时开小差（${status}），请稍后重试`;
  }
}

/**
 * 「很可能根本没到达 backend」的失败 —— **幂等**调用的重试判定口径。
 *
 * 两种情形都只能由网络/中间层造成:
 *   ① 没有响应（连接失败/被拒）；
 *   ② 5xx 且响应体**不是** PA 信封（隧道/反代/CDN 的错误页）。
 *
 * 为什么不把「所有 5xx」算进来：backend 自己的 5xx 是**信封**（如 500「服务内部错误」），
 * 那类重试无意义，还可能把小问题放大。
 * 用法限制：只给**幂等语义**的调用用 —— 例：审批定案是 CAS（`approval_service.decide`，
 * 重复定案只回 409「已被他人定案」，不会重复上架），所以重试一次是安全的。
 */
export function isTransientGatewayFailure(e: unknown): boolean {
  if (!axios.isAxiosError(e)) return false;
  if (!e.response) return true;
  return e.response.status >= 500 && !isEnvelope(e.response.data as EnvelopeBody | undefined);
}

/**
 * 把任意异常转成可读的中文提示。
 * 优先级：信封 message（PA 的业务原因）→ 429 的 Retry-After → 状态码兜底 → 网络/超时 → fallback。
 * 网络/超时两种文案会带上目标基址（见 `targetSuffix`）。
 *
 * 另一条 2026-09 真机排障口径：**5xx 且响应体不是信封**时在文案后补一句来源提示 ——
 * 那次「审批偶发 503『依赖服务暂不可用』、重试即成功」正是隧道边缘返回的，
 * 而 backend 当日 0 个 5xx；不补这句就只能看到"后端说依赖不可用"的错误结论。
 */
export function apiErrorMessage(e: unknown, fallback = "操作失败，请稍后重试"): string {
  if (axios.isAxiosError(e)) {
    const body = e.response?.data as EnvelopeBody | undefined;
    const message = body?.message;
    if (typeof message === "string" && message.trim()) return message;
    const detail = body?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;

    if (e.code === "ECONNABORTED" || (e.message || "").includes("timeout")) return `请求超时${targetSuffix()}，请稍后重试`;
    if (!e.response) return `网络异常${targetSuffix()}，请检查网络后重试`;
    const status = e.response.status;
    const base = statusMessage(status, e);
    return status >= 500 && !isEnvelope(body) ? `${base}（非后端信封响应，可能来自隧道/网关）` : base;
  }
  if (e instanceof Error && e.message) return e.message;
  return fallback;
}
