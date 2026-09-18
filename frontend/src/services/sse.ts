// SSE 客户端（**PA 语义**，与 ProductPilot 的实现在三个关键点上不同，改动前先读）：
//
// 1) 终态集合**含 `hitl.waiting`**：backend 的 `TERMINAL_EVENT_TYPES` 把它算作终态
//    （等待人工审批可能持续数小时，长连接没有意义）→ 服务端收到它就关流。
//    PP 版把 `hitl.waiting` 当「非终态」继续等 `approval.resumed`，接到 PA 上会**永远**
//    停在「断线续传中…」空转重连。这里命中终态即**停止重连**并提示「已转人工审批」。
// 2) `ready` 是 backend 生成的**控制帧**（该商品没有进行中任务）→ 显示终态提示、**不重连**。
//    否则前端会对着一个永远没有事件的流反复重连（空转）。
// 3) 注释帧（以 `:` 开头，如 `: stream-idle-close` / `: stream-error`）**不触发 onmessage**，
//    必须在文本层自己识别 —— PP 只解析 `data:` 行，因此分不清「空闲关流」（应带
//    Last-Event-ID 续连）与「读流异常」（应告警并限量重试）。
import { fetchStreamTicket } from "./http";
import { apiUrl, getPlatform, type PaSseAttempt, type PaSseOutcome } from "./platform";

/** 与 backend `services/stream_reader.TERMINAL_EVENT_TYPES` 保持一致（改一侧必须改另一侧）。 */
export const TERMINAL_EVENT_TYPES = new Set(["done", "rejected", "failed", "hitl.waiting"]);

/** 单帧解析结果：事件帧给 `{id,type,data}`，注释帧给 `{comment}`。 */
export interface SseFrame {
  id?: string;
  type?: string;
  data?: unknown;
  comment?: string;
}

/** 一次连接的走向（调用方据此决定是否重连）。定义在平台端口层，这里保持同名导出以免破坏既有调用方。 */
export type SseOutcome = PaSseOutcome;

/**
 * 解析单帧（帧内多行）。
 *
 * 参数:
 *   lines: 一帧的原始行（SSE 以空行分帧）。
 * 返回:
 *   事件帧 → `{id, type, data}`；注释帧 → `{comment}`；无效/空帧 → null。
 */
export function parseSseFrame(lines: string[]): SseFrame | null {
  let id: string | undefined;
  let dataLine: string | undefined;
  const comments: string[] = [];
  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, "");
    if (!line) continue;
    if (line.startsWith(":")) {
      comments.push(line.slice(1).trim());
      continue;
    }
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("data:")) dataLine = line.slice(5).trim();
  }
  if (comments.length > 0 && dataLine === undefined) {
    return { id, comment: comments.join(" ") };
  }
  if (dataLine === undefined) return null;
  try {
    const payload = JSON.parse(dataLine) as { type?: string; data?: unknown };
    return { id, type: payload.type, data: payload.data };
  } catch {
    return { id, type: "raw", data: dataLine };
  }
}

/**
 * 该帧决定本次连接的走向。
 *
 * 返回:
 *   `idle`（空闲关流，可续连）/ `error`（读流异常，限量重试）/ `ready`（无任务，停止）/
 *   `terminal`（终态，停止）/ null（普通事件，继续读）。
 */
export function frameOutcome(frame: SseFrame): SseOutcome | null {
  if (frame.comment) {
    if (frame.comment.includes("stream-idle-close")) return "idle";
    if (frame.comment.includes("stream-error")) return "error";
    return null; // 其它注释帧（如保活 `: ping`）忽略
  }
  if (frame.type === "ready") return "ready";
  if (frame.type && TERMINAL_EVENT_TYPES.has(frame.type)) return "terminal";
  return null;
}

/**
 * 流地址。Web 是相对路径（dev 由 vite 代理，生产由 nginx 同源反代）；
 * RN 会在前面拼上 `EXPO_PUBLIC_API_BASE_URL`（原生客户端没有代理，必须是绝对地址）。
 */
export function streamUrl(productId: string): string {
  return apiUrl(`/api/v1/products/${productId}/stream`);
}

// `consumeSse` **已移入 `./platform`**：它依赖 `response.body.getReader()`，是纯浏览器实现。
// RN 侧在 `rnPlatform.openSse` 里用 expo/fetch 实现同一契约（同样能拿到注释帧），
// 因此上面这些「什么算终态、什么时候该停」的判断**两端共用一份**。

/** 连流过程的回调面（各回调职责独立，调用方只实现关心的几个）。 */
export interface StreamHandlers {
  /** 每帧回调（注释帧也会来） */
  onFrame: (frame: SseFrame) => void;
  /** `ready` 控制帧：无进行中任务 */
  onReady?: () => void;
  /** 终态事件（done/rejected/failed/hitl.waiting） */
  onTerminal?: (type: string) => void;
  /** 空闲关流（可续连，属正常生命周期） */
  onIdle?: () => void;
  /** 网络/读流异常（限量重试） */
  onTransientError?: (reason: string) => void;
  /** 不可恢复（票据/鉴权失败、重试耗尽） */
  onFatal?: (reason: string) => void;
}

/** `connectProductStream` 的完整入参（商品 + 回调 + 重试策略 + 中断信号）。 */
export interface StreamOptions extends StreamHandlers {
  productId: string;
  /** 续连/重试间隔（毫秒；默认 1500，测试注入小值以去掉墙钟依赖） */
  retryDelayMs?: number;
  /** 网络/读流异常的最大重试次数（空闲续连**不计入**：空闲是正常生命周期） */
  maxRetries?: number;
  signal: AbortSignal;
}

/** 连接过程中的可观测量（便于测试断言与 UI 展示） */
export interface StreamState {
  lastEventId?: string;
  terminals: string[];
  idleReconnects: number;
  retries: number;
  stopped: boolean;
}

/** 重连/退避用的等待（抽成一行：测试注入 `retryDelayMs: 0` 即可去掉墙钟依赖）。 */
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 连接商品生成流（换票 → 读流 → 按走向决定续连或停止）。
 *
 * 重连策略（一句话：**该停的绝不再连，该连的绝不静默断**）:
 *   · `terminal` / `ready` → 停止（继续挂着没有意义）；
 *   · `idle` / `eof` → 立即带 `Last-Event-ID` 续连（服务端每 `STREAM_IDLE_SECONDS` 空闲关流一次）；
 *   · 网络异常 / `stream-error` → 退避重试，超过 `maxRetries` 交给 `onFatal`；
 *   · 换不到票据 → 直接 `onFatal`（多半是登录态失效，重试只是拖延）。
 */
export async function connectProductStream(options: StreamOptions): Promise<StreamState> {
  const { productId, onFrame, signal } = options;
  const retryDelay = options.retryDelayMs ?? 1500;
  const maxRetries = options.maxRetries ?? 3;
  const state: StreamState = { terminals: [], idleReconnects: 0, retries: 0, stopped: false };
  let ticket = await fetchStreamTicket(productId);
  if (!ticket) {
    state.stopped = true;
    options.onFatal?.("无法获取流票据（登录态可能已失效）");
    return state;
  }
  while (!state.stopped && !signal.aborted) {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (state.lastEventId) headers["Last-Event-ID"] = state.lastEventId;
    // 传输交给平台端口（Web = fetch + 读流；RN = expo/fetch），
    // 失败与中断都在端口内部收敛成 `{outcome, failure}` —— 因此**重连策略与平台无关**，只在这一处写。
    const attempt: PaSseAttempt = await getPlatform().openSse(
      {
        url: `${streamUrl(productId)}?ticket=${encodeURIComponent(ticket)}`,
        headers,
        lastEventId: state.lastEventId,
        signal,
      },
      (frame) => {
        if (frame.id) state.lastEventId = frame.id;
        onFrame(frame);
        if (!frame.comment && frame.type && TERMINAL_EVENT_TYPES.has(frame.type)) {
          state.terminals.push(frame.type);
        }
      },
    );
    const outcome: SseOutcome = attempt.outcome;
    const failure: string | null = attempt.failure ?? null;
    if (signal.aborted) break;

    if (outcome === "terminal") {
      state.stopped = true;
      options.onTerminal?.(state.terminals[state.terminals.length - 1] ?? "done");
      break;
    }
    if (outcome === "ready") {
      state.stopped = true;
      options.onReady?.();
      break;
    }
    if (outcome === "idle" || (outcome === "eof" && !failure)) {
      state.idleReconnects += 1;
      options.onIdle?.();
      await sleep(retryDelay);
      continue;
    }
    state.retries += 1;
    if (state.retries > maxRetries) {
      state.stopped = true;
      options.onFatal?.(failure ? `连接失败：${failure}` : "连接失败");
      break;
    }
    // 票据可能已过期（TTL 默认 120s）：重试前换一张，避免拿旧票据反复 401
    ticket = (await fetchStreamTicket(productId)) ?? ticket;
    options.onTransientError?.(failure ?? "连接中断");
    await sleep(retryDelay);
  }
  return state;
}
