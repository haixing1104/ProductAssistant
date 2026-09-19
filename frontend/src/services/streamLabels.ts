// SSE 事件 → 业务可读的阶段行（**三端唯一实现**：H5 / RN / 桌面共用，单测守护）。
//
// 位置说明（2026-09 从 `mobile-h5/src/services/streamLabels.ts` 上移到契约核心层）：
//   事件文案是**后端事件契约的读侧**，两端显示同一句话才谈得上"同源同义"；
//   2026-09 又把桌面 `StreamingDisplay` 里那份"逐条对齐"的重复实现删掉、改为 import 本文件 ——
//   那句"改一侧必须改另一侧"的注释本身就是 bug 温床：真实事故是 `agent.tool` 的键名
//   生产者发 `tool`、两份实现都读 `name` → 界面上工具名**常年空白**，而测试自造了 `name`
//   所以一直"通过"。**一份实现 + 用真实事件载荷断言**是唯一可靠的防再发方式。
//   覆盖率口径：用例随文件一起放在共享层（`frontend/src/__tests__/streamLabels.test.ts`），
//   由桌面端套件统一守护（原因见 `mobileFormat.ts` 的头部注释）。
//
// 文案红线（2026-09 用户反馈「这是非业务的数据，没人知道这是什么意思」）:
//   **阶段流只允许出现业务语言**。`stop_reason` / `turns` / `tool_calls` / 内部工具名 /
//   `score=` / `来源：uploaded` / `（Agent）` 这类实现细节一律不进业务文案 ——
//   它们仍在事件里（`evt:{thread_id}` / 日志 / `agent_trace`），排障照旧可查。
//   另：预算到顶（`model_call_limit`）在默认预算（2 轮）下是**常态**而不是异常，
//   故不占阶段流 —— 给常态打警告会训练用户无视警告，比不显示更糟。
import type { SseFrame } from "./sse";

/** 流事件类型 → 中文标题（与 backend `stream_reader` 的事件名对齐；未知类型透出原名）。 */
export const TYPE_LABELS: Record<string, string> = {
  "generate.started": "▶ 开始生成",
  "stage.researching": "🔎 正在核对商品资料…",
  "agent.tool": "🛠 已核对",
  // 「自然收敛」时的标题；**非 completed 不出行**（见 formatEvent 的 agent.done 分支）
  "agent.done": "✔ 资料核对完成",
  "stage.generating": "✎ 文案生成中…",
  "stage.evaluating": "⚖ 合规评估中…",
  "evaluate.result": "✔ 合规评估",
  "stage.imaging": "🎨 配图整理中…",
  "image.ready": "✔ AI 配图已就绪",
  "content.chunk": "",
  "hitl.waiting": "✋ 需人工审批，已转审批中心",
  "approval.resumed": "↻ 审批已回传，恢复写入",
  done: "✔ 生成完成",
  rejected: "✘ 已被驳回（已退回草稿）",
  failed: "✘ 生成失败",
  ready: "（暂无进行中任务）",
};

/**
 * 内部工具名 → 业务名（**只在这里出现一次**）。
 *
 * 为什么必须映射: 工具名（`get_product_facts`…）是实现细节，审批人/运营看不懂；他们真正要知道的是
 * "AI 参考了哪些资料"（这决定他要不要更仔细地审这版文案）。未知工具**回落「资料」**而不透出原名 ——
 * 后端新增工具时，文案不许因此变丑。
 */
const TOOL_LABELS: Record<string, string> = {
  get_product_facts: "商品素材",
  get_content_history: "历史文案",
  get_eval_history: "历史违规点",
  get_approval_history: "历史驳回意见",
  scan_compliance: "合规自查",
  retrieve_similar_copy: "同类历史文案",
};

/** 工具名 → 业务名（未知/缺失回落「资料」）。 */
export function toolLabel(tool: string | undefined): string {
  if (!tool) return "资料";
  return TOOL_LABELS[tool] ?? "资料";
}

/** 配图来源（`node_image` 发 `uploaded` / `ai`）→ 业务名；未知/缺失返回 ""（由调用方决定要不要带括号）。 */
export function sourceLabel(source: string | undefined): string {
  if (source === "uploaded") return "上传素材";
  if (source === "ai") return "AI 生成";
  return "";
}

/**
 * 把一帧转成业务可读的阶段行。
 *
 * 返回空串 = **不占阶段流**（调用方 `filter((line) => line.length > 0)`，与 `content.chunk` 同机制）：
 *   · `content.chunk` 的正文走打字机；
 *   · `agent.done` 的预算/工具到顶属常态（见文件头「文案红线」）。
 */
export function formatEvent(frame: SseFrame): string {
  if (frame.comment) return `… ${frame.comment}`;
  const label = TYPE_LABELS[frame.type ?? ""] ?? `事件：${frame.type ?? "raw"}`;
  const data = (frame.data ?? {}) as {
    score?: number | string;
    passed?: boolean;
    attempt?: number;
    ok?: boolean;
    name?: string;
    /** 生产者（`loop._tools_node`）实际发的键名是 `tool`；`name` 仅为兼容旧载荷而读。 */
    tool?: string;
    stop_reason?: string;
    result?: string;
    source?: string;
  };
  switch (frame.type) {
    case "evaluate.result":
      // 业务口径：「82 分」而不是「score=82」——字段名不该出现在给审核员看的句子里
      if (data.score === undefined || data.score === null) return `${label}：已完成`;
      return `${label}：${data.score} 分（${data.passed ? "通过" : "未通过"}）`;
    case "stage.generating":
    case "stage.evaluating":
      return data.attempt ? `${label}（第 ${data.attempt} 次）` : label;
    case "agent.tool": {
      // 键名以**生产者**为准（`tool`）：读 `name` 会让工具名恒为空（2026-09 实测 bug，见文件头）
      const tool = toolLabel(data.tool ?? data.name);
      return data.ok === false ? `${label}：${tool}（未取到）` : `${label}：${tool}`;
    }
    case "agent.done": {
      // 只把"真的出事了"说出来；预算/工具到顶（默认预算下的常态）与缺字段一律不出行
      const reason = data.stop_reason || "";
      if (reason === "completed") return label;
      if (reason === "runtime_error") return "⚠ 本次未参考历史资料（服务异常），文案按商品素材生成";
      return "";
    }
    case "stage.imaging": {
      const source = sourceLabel(data.source);
      return source ? `${label}（来源：${source}）` : label;
    }
    case "approval.resumed":
      return data.result === "rejected" ? "↻ 审批驳回，正在退回草稿…" : label;
    default:
      return label;
  }
}

/**
 * 状态行（"现在在做什么"的一句话）—— 业务口径，**未知类型不透出内部事件名**。
 *
 * 与 formatEvent 的分工: 状态行是粗粒度指示器，阶段流是明细。`agent.done` 在预算到顶时明细不出行，
 * 此时状态行也不能谎报「✔ 完成」，回落中性的「处理中…」。
 */
export function statusLine(frame: SseFrame): string {
  if (frame.comment) return `… ${frame.comment}`;
  const label = TYPE_LABELS[frame.type ?? ""];
  if (!label) return "处理中…";
  if (frame.type === "agent.done") {
    const reason = (frame.data as { stop_reason?: string } | undefined)?.stop_reason;
    return reason === "completed" ? label : "处理中…";
  }
  return label;
}
