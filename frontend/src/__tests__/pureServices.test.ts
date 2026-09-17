// 纯函数层用例：枚举映射（PA 多 preorder/deleted）、轨迹整理、通知状态、快照工具、合规高亮。
import { describe, expect, it } from "vitest";

import { reasonMeta, snapshotBlocks, snapshotText } from "../services/contentSnapshot";
import { traceRows, violationsSummary, evaluatorLabel } from "../services/evaluationTrace";
import {
  channelLabel,
  hasDeliveryFailure,
  notificationsSummary,
  statusMeta,
} from "../services/notificationStatus";
import {
  approvalStatusLabel,
  productStatusColor,
  productStatusLabel,
  severityColor,
  severityLabel,
  stockStatusLabel,
} from "../services/productMeta";
import { hitSpans, highlightHits } from "../pages/CompliancePage";
import type { ApprovalNotification, ComplianceHit, ContentSnapshot, EvaluationLog } from "../types/api";

describe("productMeta 枚举", () => {
  it("PA 的 stock_status 含 preorder（PP 没有）", () => {
    expect(stockStatusLabel("preorder")).toBe("预售");
    expect(stockStatusLabel("in_stock")).toBe("有货");
  });

  it("PA 的 status 含 deleted（历史态）且未知值原样透出", () => {
    expect(productStatusLabel("waiting_approval")).toBe("待审批");
    expect(productStatusLabel("deleted")).toContain("历史");
    expect(productStatusLabel("brand_new_state")).toBe("brand_new_state");
    expect(productStatusColor("generating")).toBe("processing");
  });

  it("审批状态与严重级标签", () => {
    expect(approvalStatusLabel("rejected")).toBe("已驳回");
    expect(severityLabel("high")).toContain("阻断");
    expect(severityColor("medium")).toBe("orange");
    expect(severityColor("unknown")).toBe("default");
  });
});

describe("evaluationTrace", () => {
  it("评估层标签：rule/llm 中文，未知原样", () => {
    expect(evaluatorLabel("rule")).toContain("规则层");
    expect(evaluatorLabel("llm")).toContain("LLM");
    expect(evaluatorLabel(undefined)).toBe("未知评估层");
  });

  it("违规摘要兼容字符串与 {keyword,reason} 两种形状", () => {
    expect(violationsSummary(["命中违禁词「国家级」"])).toBe("命中违禁词「国家级」");
    expect(violationsSummary([{ keyword: "100%", reason: "需补充依据" }])).toBe("100%（需补充依据）");
    expect(violationsSummary([{ reason: "仅原因" }])).toBe("仅原因");
    expect(violationsSummary("not-array")).toBe("");
  });

  it("轨迹行按正序编号，缺失分数/耗时为 null（不显示 0）", () => {
    const logs: EvaluationLog[] = [
      { id: "a", thread_id: "t", evaluator_type: "rule", score: 20, errors: ["x"], latency_ms: 3 },
      { id: "b", thread_id: "t", evaluator_type: "llm", score: null, errors: [], latency_ms: null },
    ];
    const rows = traceRows(logs);
    expect(rows.map((r) => r.attempt)).toEqual([1, 2]);
    expect(rows[0].latencyMs).toBe(3);
    expect(rows[1].score).toBeNull();
    expect(rows[1].createdAt).toBeNull();
    expect(traceRows(undefined)).toEqual([]);
  });
});

describe("notificationStatus", () => {
  const note = (over: Partial<ApprovalNotification>): ApprovalNotification => ({
    channel: "dingtalk",
    status: "pending",
    retry_count: 0,
    ...over,
  });

  it("状态文案：sent/dlq/pending 与重试中", () => {
    expect(statusMeta(note({ status: "sent" })).label).toBe("已发送");
    expect(statusMeta(note({ status: "dlq", retry_count: 3 })).label).toBe("投递失败");
    expect(statusMeta(note({ retry_count: 2 })).label).toContain("第 2 次");
    expect(statusMeta(note({})).label).toBe("待投递");
  });

  it("渠道中文名与摘要文案", () => {
    expect(channelLabel("feishu")).toBe("飞书");
    expect(channelLabel("wecom")).toBe("wecom");
    expect(notificationsSummary(undefined)).toBe("未配置外部通知");
    expect(notificationsSummary([note({ status: "sent" })])).toBe("钉钉 已发送");
  });

  it("hasDeliveryFailure 只认 dlq", () => {
    expect(hasDeliveryFailure([note({ status: "dlq" })])).toBe(true);
    expect(hasDeliveryFailure([note({ status: "sent" })])).toBe(false);
    expect(hasDeliveryFailure(undefined)).toBe(false);
  });
});

describe("contentSnapshot", () => {
  const snapshot: ContentSnapshot = {
    reason: "high_value",
    content: { blocks: [{ type: "text", text: "第一段" }, { type: "image", url: "https://x/1.png" }] },
    evaluation_result: { score: 88 },
  };

  it("取纯文本与 blocks（图片 block 不计入文本）", () => {
    expect(snapshotText(snapshot)).toBe("第一段");
    expect(snapshotBlocks(snapshot)).toHaveLength(2);
    expect(snapshotText(null)).toBe("");
    expect(snapshotBlocks(undefined)).toEqual([]);
  });

  it("转人工原因 → 中文标签（未知原因返回 null）", () => {
    expect(reasonMeta("high_value")?.label).toContain("高价商品");
    expect(reasonMeta("quality_exhausted")?.color).toBe("volcano");
    expect(reasonMeta("whatever")).toBeNull();
  });
});

describe("合规预览高亮", () => {
  const hit = (start: number, end: number): ComplianceHit => ({
    rule_id: "r",
    kind: "word",
    keyword: "x",
    severity: "high",
    reason: "r",
    start,
    end,
    blocking: true,
  });

  it("hitSpans 过滤非法区间并按起点排序", () => {
    expect(hitSpans([hit(5, 8), hit(0, 2), hit(3, 3)])).toEqual([
      [0, 2],
      [5, 8],
    ]);
  });

  it("highlightHits 用【】标出命中片段；无命中时原样返回", () => {
    expect(highlightHits("本店国家级最低价", [hit(2, 5)])).toBe("本店【国家级】最低价");
    expect(highlightHits("干净文案", [])).toBe("干净文案");
  });
});
