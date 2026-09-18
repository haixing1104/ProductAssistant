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
//     否则用户只知道「被限流了」却不知道该等 30 秒还是 15 分钟。
import axios from "axios";

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
 * 把任意异常转成可读的中文提示。
 * 优先级：信封 message（PA 的业务原因）→ 429 的 Retry-After → 状态码兜底 → 网络/超时 → fallback。
 */
export function apiErrorMessage(e: unknown, fallback = "操作失败，请稍后重试"): string {
  if (axios.isAxiosError(e)) {
    const body = e.response?.data as EnvelopeBody | undefined;
    const message = body?.message;
    if (typeof message === "string" && message.trim()) return message;
    const detail = body?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;

    if (e.code === "ECONNABORTED" || (e.message || "").includes("timeout")) return "请求超时，请稍后重试";
    if (!e.response) return "网络异常，请检查网络后重试";
    switch (e.response.status) {
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
        return `服务暂时开小差（${e.response.status}），请稍后重试`;
    }
  }
  if (e instanceof Error && e.message) return e.message;
  return fallback;
}
