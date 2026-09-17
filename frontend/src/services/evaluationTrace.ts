// AI 思考轨迹展示小工具（纯函数，无 antd 依赖 → jsdom 下可直接单测）。
// 数据源：backend 只读端点 `GET /products/{id}/evaluation-logs`（`schema_pa_ai.evaluation_logs`）。
import type { EvaluationLog } from "../types/api";

export interface TraceRow {
  key: string;
  /** 第几次评估（按接口返回的时间正序，即重试次序） */
  attempt: number;
  evaluatorLabel: string;
  /** 得分：规则层无 LLM 打分 → null */
  score: number | null;
  violations: string;
  latencyMs: number | null;
  createdAt: string | null;
}

const EVALUATOR_LABELS: Record<string, string> = {
  rule: "规则层（违禁词/极限词）",
  llm: "LLM 语义评估",
};

/** 评估层 → 中文名（未知类型原样透出，避免契约新增时前端崩）。 */
export function evaluatorLabel(type: string | undefined): string {
  if (!type) return "未知评估层";
  return EVALUATOR_LABELS[type] ?? type;
}

/**
 * 违规点摘要：兼容三种形状 ——
 *   · 字符串（ai-engine 规则层把 `h.reason` 直接塞进 errors）；
 *   · `{keyword, reason}`（LLM 层的 violations）；
 *   · `{keyword}` / `{reason}` 单字段。
 */
export function violationsSummary(errors: EvaluationLog["errors"] | undefined): string {
  if (!Array.isArray(errors)) return "";
  return errors
    .map((e) => {
      if (typeof e === "string") return e;
      const item = e as { keyword?: string; reason?: string };
      if (item.keyword && item.reason) return `${item.keyword}（${item.reason}）`;
      return item.keyword ?? item.reason ?? "";
    })
    .filter((t) => t.length > 0)
    .join("、");
}

/** 原始日志 → 表格行（保持接口返回的正序；缺失分数/耗时按 null 展示，不显示 0 以免误读）。 */
export function traceRows(logs: EvaluationLog[] | undefined): TraceRow[] {
  return (logs ?? []).map((log, index) => ({
    key: log.id,
    attempt: index + 1,
    evaluatorLabel: evaluatorLabel(log.evaluator_type),
    score: log.score ?? null,
    violations: violationsSummary(log.errors),
    latencyMs: log.latency_ms ?? null,
    createdAt: log.created_at ?? null,
  }));
}
