// SSE 事件 → 阶段行的用例（**H5 与 RN 共用这一份**）。
//
// 为什么单独守护: 阶段行是 SSE 事件的唯一"人话翻译"，漏一个事件用户就只看到「阶段：xxx」；
// 而 PA 的三个关键事件（`hitl.waiting` / `done` / `ready`）的**终态语义就是靠文案传达的**。
// 位置：随被测文件一起在共享层，由桌面端套件守护（原因见 `mobileFormat.test.ts` 头部注释）。
import { describe, expect, it } from "vitest";

import { formatEvent, TYPE_LABELS } from "../services/streamLabels";

describe("formatEvent（SSE 事件 → 阶段行）", () => {
  it("正文 chunk 不占阶段流（返回空串）", () => {
    expect(formatEvent({ type: "content.chunk", data: { text: "abc" } })).toBe("");
  });

  it("评估结果带上分数与是否通过（审核员判断「为什么被拦」的第一眼信息）", () => {
    expect(formatEvent({ type: "evaluate.result", data: { score: 82, passed: false } })).toBe(
      "✔ 评估结果：score=82（未过）",
    );
  });

  it("重试次数 / Agent 工具 / 配图来源都体现在行里", () => {
    expect(formatEvent({ type: "stage.generating", data: { attempt: 2 } })).toBe("✎ 文案生成中…（第 2 次）");
    expect(formatEvent({ type: "agent.tool", data: { name: "search_products", ok: false } })).toBe(
      "🛠 AI 调用工具：search_products （失败）",
    );
    expect(formatEvent({ type: "stage.imaging", data: { source: "uploaded" } })).toBe(
      "🎨 配图整理/生成中…（来源：uploaded）",
    );
  });

  it("注释帧与未知事件都给可读文案（不抛错）", () => {
    expect(formatEvent({ comment: "stream-idle-close" })).toBe("… stream-idle-close");
    expect(formatEvent({ type: "brand.new.event" })).toBe("事件：brand.new.event");
  });

  it("缺字段时用 `-` 兜底（事件契约新增字段不能让文案变成 undefined）", () => {
    expect(formatEvent({ type: "agent.tool", data: {} })).toBe("🛠 AI 调用工具：");
    expect(formatEvent({ type: "agent.done", data: {} })).toBe("✔ 数据核对完成（stop_reason=-）");
    expect(formatEvent({ type: "stage.imaging", data: {} })).toBe("🎨 配图整理/生成中…（来源：-）");
    expect(formatEvent({ type: "stage.evaluating", data: {} })).toBe("⚖ 合规评估中…");
  });

  it("评估通过 / 未通过两种结果都要说清（审核员据此决定是否放行）", () => {
    expect(formatEvent({ type: "evaluate.result", data: { score: 95, passed: true } })).toBe(
      "✔ 评估结果：score=95（通过）",
    );
  });

  it("审批回传：驳回要说清「正在退回草稿」，其它结果保持中性文案", () => {
    expect(formatEvent({ type: "approval.resumed", data: { result: "rejected" } })).toBe(
      "↻ 审批驳回，正在退回草稿…",
    );
    expect(formatEvent({ type: "approval.resumed", data: { result: "approved" } })).toBe(
      "↻ 审批已回传，恢复写入",
    );
  });

  it("PA 的三个关键事件文案在表里（终态语义靠它们传达）", () => {
    expect(TYPE_LABELS["hitl.waiting"]).toContain("已转审批中心");
    expect(TYPE_LABELS.done).toBe("✔ 生成完成");
    expect(TYPE_LABELS.ready).toContain("暂无进行中任务");
  });
});
