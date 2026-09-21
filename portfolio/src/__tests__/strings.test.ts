import { STRINGS } from "../data/strings";
import { LANGS } from "../lib/i18n";

// UI 文案字典的**对齐门禁**。
//
// 结构对齐的**编译期**那一半已经在 `data/strings.ts` 里由 `const en: typeof zh` 保证了
// （漏 key / 值类型不符 / 数组长度不同 / 函数签名不一致 → `npx tsc --noEmit` 红）。
// 这里补的是 tsc 管不了的那一半：
//   · 空字符串（key 在、值却是空的 → 页面上是一块空白，比报错更难发现）；
//   · 英文包里残留中文（漏译的机械门禁）；
//   · 中文包被误写成英文（两种语言变成同一份 → 切了没反应）。

/** 中日韩字符 + 全角标点（半角字符如 `·` `✓` `→` 不算）。 */
const CJK = /[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]/;

/** 函数类文案（aria / 版权行）的样例参数：按参数个数取够用即可。 */
const SAMPLE_ARGS = ["Sample", "Label"];

/** 递归收集「路径 → 文案」；函数会按参数个数用样例参数调用，带参文案同样进入检查范围。 */
function leaves(node: unknown, path = ""): Array<[string, string]> {
  if (typeof node === "string") return [[path, node]];
  if (typeof node === "function") {
    const fn = node as (...args: unknown[]) => unknown;
    return [[path, String(fn(...SAMPLE_ARGS.slice(0, fn.length)))]];
  }
  if (Array.isArray(node)) {
    return node.flatMap((item, index) => leaves(item, `${path}[${index}]`));
  }
  if (node && typeof node === "object") {
    return Object.entries(node).flatMap(([key, value]) => leaves(value, path ? `${path}.${key}` : key));
  }
  return [];
}

/** 递归收集「路径 → 形状」：比对键、值类型、数组长度、函数参数个数。 */
function shape(node: unknown, path = ""): Record<string, string> {
  const kind =
    typeof node === "string"
      ? "string"
      : typeof node === "function"
        ? `function/${(node as (...args: unknown[]) => unknown).length}`
        : Array.isArray(node)
          ? `array/${node.length}`
          : node && typeof node === "object"
            ? "object"
            : typeof node;

  const self = { [path]: kind };
  if (Array.isArray(node)) {
    return node.reduce((acc, item, index) => ({ ...acc, ...shape(item, `${path}[${index}]`) }), self);
  }
  if (node && typeof node === "object") {
    return Object.entries(node).reduce(
      (acc, [key, value]) => ({ ...acc, ...shape(value, path ? `${path}.${key}` : key) }),
      self,
    );
  }
  return self;
}

describe("UI 文案字典（src/data/strings.ts）", () => {
  it("字典的语言键与 LANGS 一致（加了第三种语言就该两处一起改）", () => {
    expect(Object.keys(STRINGS).sort()).toEqual([...LANGS].sort());
  });

  it("两种语言结构完全一致：键 / 值类型 / 数组长度 / 函数参数个数", () => {
    expect(shape(STRINGS.en)).toEqual(shape(STRINGS.zh));
  });

  it("两种语言都没有空文案（key 在、值是空串 → 页面上一块空白）", () => {
    for (const lang of LANGS) {
      for (const [path, text] of leaves(STRINGS[lang])) {
        expect(text.trim(), `${lang}.${path} 是空的`).not.toBe("");
      }
    }
  });

  it("中文包必须有中文（两种语言被写成同一份时，切语言看起来毫无反应）", () => {
    expect(leaves(STRINGS.zh).some(([, text]) => CJK.test(text))).toBe(true);
  });

  it("三条首屏事实两种语言都是 3 条（少一条不会报错，只会少一句话）", () => {
    expect(STRINGS.zh.hero.facts).toHaveLength(3);
    expect(STRINGS.en.hero.facts).toHaveLength(3);
  });

  // 这条就是「漏译」的机械门禁：改英文包时只改了一半、或新加一条中文文案忘了配英文，
  // 都会在这里变红 —— 而页面上「少一句英文」是没人会发现的。
  it("英文包不得残留中文；唯一豁免是语言开关上的「中」（语言名不翻译）", () => {
    for (const [path, text] of leaves(STRINGS.en)) {
      if (path === "langSwitch.zh") {
        // 豁免项本身也要钉住：谁把它改成别的写法，就该显式改这条豁免，而不是悄悄放过
        expect(text).toBe("中");
        continue;
      }
      expect(CJK.test(text), `en.${path} 还有中文未翻译 → ${text}`).toBe(false);
    }
  });

  it("原生端 Toast 文案：说明暂未开放，并给出替代路径（不是一句「不行」）", () => {
    expect(STRINGS.zh.card.toastAppDownload).toContain("暂未开放");
    expect(STRINGS.zh.card.toastAppDownload).toContain("Mobile H5");
    expect(STRINGS.en.card.toastAppDownload).toContain("Mobile H5");
  });
});
