// 组织名记忆 + 阶段行格式化（移动端专属纯函数）。
//
// 为什么单独守护: ① 记住组织名是手机上唯一的"免手打"便利，隐私模式/禁用 storage 时**必须优雅退化**
//（抛错会让整个登录页白屏）；② 阶段行是 SSE 事件的唯一人话翻译，漏一个事件用户就只看到"阶段：xxx"。
import { describe, expect, it } from "vitest";

import { forgetOrg, readRememberedOrg, rememberOrg } from "../services/rememberOrg";
import { formatEvent, TYPE_LABELS } from "../services/streamLabels";

/** 最小 Storage 替身（jsdom 有 localStorage，但这里要覆盖"抛错"与"不存在"两种真实场景）。 */
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => map.get(k) ?? null,
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    removeItem: (k: string) => void map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, v),
  } as Storage;
}

describe("rememberOrg（记住组织名）", () => {
  it("记住后能读回；空白值不写入", () => {
    const s = fakeStorage();
    rememberOrg("  示例科技  ", s);
    expect(readRememberedOrg(s)).toBe("示例科技");
    rememberOrg("   ", s);
    expect(readRememberedOrg(s)).toBe("示例科技");
  });

  it("清除后读回空串", () => {
    const s = fakeStorage();
    rememberOrg("示例科技", s);
    forgetOrg(s);
    expect(readRememberedOrg(s)).toBe("");
  });

  it("storage 不可用（隐私模式/抛错）时静默退化，不抛错", () => {
    const broken = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(readRememberedOrg(broken)).toBe("");
    expect(() => rememberOrg("示例科技", broken)).not.toThrow();
    expect(() => forgetOrg(broken)).not.toThrow();
    // null（无 window）同样不抛
    expect(readRememberedOrg(null)).toBe("");
    expect(() => rememberOrg("x", null)).not.toThrow();
  });
});

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

  it("PA 的三个关键事件文案在表里（终态语义靠它们传达）", () => {
    expect(TYPE_LABELS["hitl.waiting"]).toContain("已转审批中心");
    expect(TYPE_LABELS.done).toBe("✔ 生成完成");
    expect(TYPE_LABELS.ready).toContain("暂无进行中任务");
  });
});
