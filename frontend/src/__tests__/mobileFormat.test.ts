// 移动端展示工具的用例（纯函数；**H5 与 RN 共用这一份**）。
//
// 位置说明（2026-09 随被测文件一起从 mobile-h5/src/__tests__ 上移到共享层）:
//   覆盖率门禁必须"测试在哪、门禁在哪" —— 两个文件搬进共享层后，用例也跟着搬，
//   由**桌面端套件**（`frontend/vite.config.ts` 的 `include: src/services/**`）统一守护。
//   留在 H5 侧会造成"文件在 root 之外 → v8 覆盖默认不统计 → 谁都没守"的盲区（实测验证过）。
//
// 重点守护 toTagColor：它是「桌面 antd 颜色 → 移动端语义色」的唯一翻译层，
// 一旦漏映射，Tag 会**静默变成灰色**（不报错），而审批列表全靠 Tag 颜色扫视找异常。
import { describe, expect, it } from "vitest";

import {
  formatPrice,
  formatSeconds,
  formatTime,
  relativeTime,
  toTagColor,
  truncate,
} from "../services/mobileFormat";

describe("toTagColor（antd 预设色 → 移动端语义色）", () => {
  it("把共享层的评分/状态色翻译成移动端预设色", () => {
    // scoreColor ≥90 给 green、≥80 给 gold、<80 给 red；productStatusColor 用 processing/warning/success
    expect(toTagColor("green")).toBe("success");
    expect(toTagColor("gold")).toBe("warning");
    expect(toTagColor("red")).toBe("danger");
    expect(toTagColor("processing")).toBe("warning");
    expect(toTagColor("warning")).toBe("warning");
    expect(toTagColor("success")).toBe("success");
    expect(toTagColor("error")).toBe("danger");
    expect(toTagColor("volcano")).toBe("warning");
    expect(toTagColor("blue")).toBe("primary");
    expect(toTagColor("primary")).toBe("primary");
  });

  it("未知/空值回落 default（绝不抛错、绝不漏成 undefined）", () => {
    expect(toTagColor("default")).toBe("default");
    expect(toTagColor(undefined)).toBe("default");
    expect(toTagColor(null)).toBe("default");
    expect(toTagColor("magenta")).toBe("default");
  });
});

describe("formatTime / relativeTime", () => {
  // 用**本地时间**构造（不带 Z）：formatTime 输出的是本地时间，用 UTC 基准会让断言依赖时区
  const now = new Date("2026-09-17T12:00:00");

  it("同年省略年份，跨年带年份；非法值给 -", () => {
    expect(formatTime("2026-03-04T05:06:00", now)).toBe("03-04 05:06");
    expect(formatTime("2025-12-31T23:59:00", now)).toBe("2025-12-31 23:59");
    expect(formatTime(null, now)).toBe("-");
    expect(formatTime("not-a-date", now)).toBe("-");
  });

  it("相对时间用于审批列表（'压了多久'比绝对时间有用）", () => {
    expect(relativeTime(new Date(now.getTime() - 30_000).toISOString(), now)).toBe("刚刚");
    expect(relativeTime(new Date(now.getTime() - 5 * 60_000).toISOString(), now)).toBe("5 分钟前");
    expect(relativeTime(new Date(now.getTime() - 3 * 3600_000).toISOString(), now)).toBe("3 小时前");
    // 超过一天退回绝对时间
    expect(relativeTime("2026-09-10T08:00:00", now)).toBe("09-10 08:00");
    // 时钟漂移（未来时间）不显示「-3 分钟前」这种误导文案
    expect(relativeTime(new Date(now.getTime() + 60_000).toISOString(), now)).not.toContain("前");
  });
});

describe("formatPrice / truncate / formatSeconds", () => {
  it("价格兼容 numeric 以字符串返回的情况", () => {
    expect(formatPrice(3999)).toBe("¥ 3999");
    expect(formatPrice("3999.5")).toBe("¥ 3999.5");
    expect(formatPrice(null)).toBe("-");
    expect(formatPrice("abc")).toBe("-");
  });

  it("长文本截断（正则、错误原因在卡片里用）", () => {
    expect(truncate("abcdef", 3)).toBe("abc…");
    expect(truncate("abc", 3)).toBe("abc");
    expect(truncate(undefined)).toBe("");
  });

  it("秒 → 可读（心跳 TTL / 节流窗口）", () => {
    expect(formatSeconds(30)).toBe("30 秒");
    expect(formatSeconds(90)).toBe("1 分钟");
    expect(formatSeconds(7200)).toBe("2 小时");
    expect(formatSeconds(null)).toBe("-");
  });
});
