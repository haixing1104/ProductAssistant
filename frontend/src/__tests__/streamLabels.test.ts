// SSE 事件 → 业务阶段行的用例（**三端共用一份实现**：H5 / RN / 桌面都 import 本层）。
//
// 为什么单独守护: 阶段行是 SSE 事件的唯一"人话翻译"，漏一个事件用户就只看到「事件：xxx」；
// 而 PA 的三个关键事件（`hitl.waiting` / `done` / `ready`）的**终态语义就是靠文案传达的**。
// 本文件两条硬规矩（都是 2026-09 实测事故换来的）:
//   ① **用真实事件载荷断言，不许自造字段名** —— `agent.tool` 的键名是 `tool`
//      （生产者 `loop._tools_node`），旧用例自造了 `name` 于是"测试全绿"，而界面上工具名常年空白；
//   ② **阶段流只出现业务语言** —— 显式断言排除 `stop_reason` / `turns` / `tool_calls` /
//      内部工具名 / `score=` / 来源枚举（用户原话："这是非业务的数据，没人知道这是什么意思"）。
import { describe, expect, it } from "vitest";

import { formatEvent, statusLine, TYPE_LABELS } from "../services/streamLabels";

describe("formatEvent（SSE 事件 → 业务阶段行）", () => {
  it("正文 chunk 不占阶段流（返回空串）", () => {
    expect(formatEvent({ type: "content.chunk", data: { text: "abc" } })).toBe("");
  });

  it("评估结果用业务口径说分数（不暴露 score= 字段名）", () => {
    expect(formatEvent({ type: "evaluate.result", data: { score: 82, passed: false } })).toBe(
      "✔ 合规评估：82 分（未通过）",
    );
    expect(formatEvent({ type: "evaluate.result", data: { score: 95, passed: true } })).toBe(
      "✔ 合规评估：95 分（通过）",
    );
    expect(formatEvent({ type: "evaluate.result", data: { passed: true } })).toBe("✔ 合规评估：已完成");
  });

  it("agent.tool 用**真实载荷**（键名 `tool`）说清「参考了哪些资料」", () => {
    // 真实事件原文（从 dev 的 evt 流抄下来）:
    // {"type":"agent.tool","data":{"thread_id":"…","tool":"get_product_facts","ok":true}}
    expect(
      formatEvent({
        type: "agent.tool",
        data: { thread_id: "t-1", tool: "get_product_facts", ok: true },
      }),
    ).toBe("🛠 已核对：商品素材");
    expect(formatEvent({ type: "agent.tool", data: { tool: "get_eval_history", ok: false } })).toBe(
      "🛠 已核对：历史违规点（未取到）",
    );
    // 后端新增工具：回落「资料」，不透出内部工具名
    expect(formatEvent({ type: "agent.tool", data: { tool: "brand_new_tool", ok: true } })).toBe(
      "🛠 已核对：资料",
    );
    // 旧载荷（`name`）仍兼容，但只在没有 `tool` 时生效
    expect(formatEvent({ type: "agent.tool", data: { name: "scan_compliance" } })).toBe(
      "🛠 已核对：合规自查",
    );
  });

  it("agent.done：只有自然收敛才报完成；预算到顶（常态）不出行；异常说业务影响", () => {
    expect(
      formatEvent({ type: "agent.done", data: { stop_reason: "completed", turns: 2, tool_calls: 1 } }),
    ).toBe("✔ 资料核对完成");
    // 用户实测形态：默认预算（2 轮）用尽 → 常态，不占阶段流（也绝不能写成"完成"）
    expect(
      formatEvent({ type: "agent.done", data: { stop_reason: "model_call_limit", turns: 2, tool_calls: 1 } }),
    ).toBe("");
    expect(
      formatEvent({ type: "agent.done", data: { stop_reason: "tool_call_limit", turns: 2, tool_calls: 3 } }),
    ).toBe("");
    // 真正出事了才说，且说的是业务影响而不是内部 stop_reason
    expect(
      formatEvent({ type: "agent.done", data: { stop_reason: "runtime_error", turns: 1, tool_calls: 0 } }),
    ).toBe("⚠ 本次未参考历史资料（服务异常），文案按商品素材生成");
    // 缺字段/空串 = 判不出原因 → 同样不出行（且不能渲染成「stop_reason=」）
    expect(formatEvent({ type: "agent.done", data: {} })).toBe("");
    expect(formatEvent({ type: "agent.done", data: { stop_reason: "" } })).toBe("");
  });

  it("配图来源翻译成业务名（不出现 uploaded / ai 这类枚举）", () => {
    expect(formatEvent({ type: "stage.imaging", data: { source: "uploaded", count: 2 } })).toBe(
      "🎨 配图整理中…（来源：上传素材）",
    );
    expect(formatEvent({ type: "stage.imaging", data: { source: "ai" } })).toBe(
      "🎨 配图整理中…（来源：AI 生成）",
    );
    // 来源未知：不带括号，也不出现「来源：-」
    expect(formatEvent({ type: "stage.imaging", data: {} })).toBe("🎨 配图整理中…");
  });

  it("重试次数体现为「第 N 次」（审核员据此判断是否在反复重写）", () => {
    expect(formatEvent({ type: "stage.generating", data: { attempt: 2 } })).toBe("✎ 文案生成中…（第 2 次）");
    expect(formatEvent({ type: "stage.evaluating", data: {} })).toBe("⚖ 合规评估中…");
  });

  it("注释帧与未知事件都给可读文案（不抛错）", () => {
    expect(formatEvent({ comment: "stream-idle-close" })).toBe("… stream-idle-close");
    expect(formatEvent({ type: "brand.new.event" })).toBe("事件：brand.new.event");
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

  it("畸形/缺字段的帧不崩、也不渲染出 undefined / raw", () => {
    // 工具名缺失（事件被裁过/老版本）：回落「资料」，绝不出现「🛠 已核对：」这种半截文案
    expect(formatEvent({ type: "agent.tool", data: {} })).toBe("🛠 已核对：资料");
    // 连 type 都没有：走兜底且逐字可读
    expect(formatEvent({})).toBe("事件：raw");
    expect(statusLine({})).toBe("处理中…");
    expect(statusLine({ comment: "stream-idle-close" })).toBe("… stream-idle-close");
  });
});

describe("statusLine（状态行：不透出内部事件名）", () => {
  it("已知事件给业务短句", () => {
    expect(statusLine({ type: "stage.researching", data: { tools: ["get_product_facts"] } })).toBe(
      "🔎 正在核对商品资料…",
    );
    expect(statusLine({ type: "agent.tool", data: { tool: "get_product_facts", ok: true } })).toBe(
      "🛠 已核对",
    );
  });

  it("agent.done 只在自然收敛时报完成，常态回落中性文案（不谎报完成）", () => {
    expect(statusLine({ type: "agent.done", data: { stop_reason: "completed" } })).toBe("✔ 资料核对完成");
    expect(statusLine({ type: "agent.done", data: { stop_reason: "model_call_limit" } })).toBe("处理中…");
  });

  it("未知事件不许把内部事件名当状态显示", () => {
    expect(statusLine({ type: "brand.new.event" })).toBe("处理中…");
  });
});
