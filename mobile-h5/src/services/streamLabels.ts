// SSE 事件 → 中文阶段行（移动端共用纯函数，单测守护）。
//
// 与桌面 `StreamingDisplay.formatEvent` 逐条对齐（同一份事件契约，改一侧必须改另一侧）:
//   15 类事件 + `ready` 控制帧；`content.chunk` 是打字机正文不进阶段流。
import type { SseFrame } from "@pa/core/services/sse";

export const TYPE_LABELS: Record<string, string> = {
  "generate.started": "▶ 开始生成",
  "stage.researching": "🔎 正在核对数据（Agent）",
  "agent.tool": "🛠 AI 调用工具",
  "agent.done": "✔ 数据核对完成",
  "stage.generating": "✎ 文案生成中…",
  "stage.evaluating": "⚖ 合规评估中…",
  "evaluate.result": "✔ 评估结果",
  "stage.imaging": "🎨 配图整理/生成中…",
  "image.ready": "✔ AI 配图已就绪",
  "content.chunk": "",
  "hitl.waiting": "✋ 需人工审批，已转审批中心",
  "approval.resumed": "↻ 审批已回传，恢复写入",
  done: "✔ 生成完成",
  rejected: "✘ 已被驳回（已退回草稿）",
  failed: "✘ 生成失败",
  ready: "（暂无进行中任务）",
};

/** 把一帧转成可读的阶段行（正文 chunk 返回空串，不占阶段流）。 */
export function formatEvent(frame: SseFrame): string {
  if (frame.comment) return `… ${frame.comment}`;
  const label = TYPE_LABELS[frame.type ?? ""] ?? `事件：${frame.type ?? "raw"}`;
  const data = (frame.data ?? {}) as {
    score?: number | string;
    passed?: boolean;
    attempt?: number;
    ok?: boolean;
    name?: string;
    stop_reason?: string;
    result?: string;
    source?: string;
  };
  switch (frame.type) {
    case "evaluate.result":
      return `${label}：score=${data.score}${data.passed ? "（通过）" : "（未过）"}`;
    case "stage.generating":
    case "stage.evaluating":
      return data.attempt ? `${label}（第 ${data.attempt} 次）` : label;
    case "agent.tool":
      return `${label}：${data.name ?? ""} ${data.ok === false ? "（失败）" : ""}`.trim();
    case "agent.done":
      return `${label}（stop_reason=${data.stop_reason ?? "-"}）`;
    case "stage.imaging":
      return `${label}（来源：${data.source ?? "-"}）`;
    case "approval.resumed":
      return data.result === "rejected" ? "↻ 审批驳回，正在退回草稿…" : label;
    default:
      return label;
  }
}
