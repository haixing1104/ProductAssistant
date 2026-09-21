import { fireEvent, render, screen, within } from "@testing-library/react";

import App from "../App";
import { projects } from "../data/projects";
import { projectsEn } from "../data/projects.en";
import { STRINGS } from "../data/strings";
import { contact } from "../lib/contact";
import { STORAGE_KEY } from "../lib/i18n";

// 右上角语言开关的**页面级**回归：默认中文 → 一键切英文（整页文案 + 作品内容 + `<html lang>`
// + 落盘）→ 再切回中文；外加「上次选过英文 → 首帧就是英文」的持久化行为。
//
// 这是「切了语言会不会有一半还是中文」的唯一机械保障：分区域逐组断言，
// 再加一条「正文不得残留中日韩字符」的总闸（漏译任意一条具体文案都会红）。

const zh = STRINGS.zh;
const en = STRINGS.en;

/** 中日韩字符 + 全角标点（半角 `·` `✓` `→` 不算）。 */
const CJK = /[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]/;

/** 点击语言开关上的某个按钮（「中」/「EN」）。 */
const pickLang = (label: string) => fireEvent.click(screen.getByRole("button", { name: label }));

/** 取某个语言下的语言开关这一组（页面上唯一带 aria-label 的 role=group）。 */
const langGroup = (label: string) => screen.getByRole("group", { name: label });

/**
 * 收集子树里**每行自己的**文本 + `aria-label` / `title` / `alt`。
 *
 * 为什么不用 `textContent`：它会把被排除子树（语言开关）里的文字也算进来。
 * 为什么要收属性：切语言时屏幕阅读器读的 `aria-label`、以及图片加载失败时显示的 `alt`，
 * 都必须跟着换 —— 这类文案漏译在页面上完全看不出来。
 */
function textAndLabels(root: HTMLElement, exclude: HTMLElement | null): string[] {
  const parts: string[] = [];
  const nodes = [root, ...Array.from(root.querySelectorAll<HTMLElement>("*"))];

  for (const element of nodes) {
    if (exclude && (element === exclude || exclude.contains(element))) continue;

    for (const child of Array.from(element.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) parts.push(child.textContent ?? "");
    }
    for (const attribute of ["aria-label", "title", "alt"]) {
      const value = element.getAttribute(attribute);
      if (value) parts.push(value);
    }
  }

  return parts;
}

// 语言落在 localStorage 上：每个用例都从「没选过语言」开始（否则前一个用例的选择会串进来）
beforeEach(() => {
  window.localStorage.clear();
});

describe("语言开关：位置与默认值", () => {
  it("开关在首屏（banner）右上角那一行：两段 `中` / `EN`，默认选中「中」", () => {
    render(<App />);

    const group = within(screen.getByRole("banner")).getByRole("group", { name: zh.langSwitch.group });
    const buttons = within(group).getAllByRole("button");

    expect(buttons.map((button) => button.textContent)).toEqual(["中", "EN"]);
    expect(within(group).getByRole("button", { name: "中" })).toHaveAttribute("aria-pressed", "true");
    expect(within(group).getByRole("button", { name: "EN" })).toHaveAttribute("aria-pressed", "false");
    // 每个按钮各带自己的 lang：屏幕阅读器才会用对应语言的发音规则去念「EN」
    expect(within(group).getByRole("button", { name: "中" })).toHaveAttribute("lang", "zh-CN");
    expect(within(group).getByRole("button", { name: "EN" })).toHaveAttribute("lang", "en");
  });

  it("默认中文：首屏、作品卡、页脚都是中文，`<html lang>` 与标签页标题也是中文那份", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(zh.hero.title);
    expect(screen.getByRole("link", { name: zh.hero.cta })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: projects[0].name })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(zh.footer.heading);

    expect(document.documentElement.lang).toBe("zh-CN");
    expect(document.title).toBe(zh.meta.title);
  });
});

describe("切到英文：整页一起换", () => {
  it("点 EN 后首屏 / 作品卡 / 演示区 / 页脚全部变英文，且 `<html lang>`、标题、localStorage 一起变", () => {
    const { container } = render(<App />);
    pickLang("EN");

    // 开关自身的选中态也翻过来
    const group = langGroup(en.langSwitch.group);
    expect(within(group).getByRole("button", { name: "EN" })).toHaveAttribute("aria-pressed", "true");

    // 首屏（文案 + 三条事实）
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(en.hero.title);
    expect(screen.getByRole("link", { name: en.hero.cta })).toBeInTheDocument();
    for (const fact of en.hero.facts) {
      expect(screen.getByText(fact.label)).toBeInTheDocument();
    }

    // 作品卡：**内容数据**也换成英文那一份（name / highlights）
    expect(screen.getByRole("heading", { level: 3, name: projectsEn[0].name })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3, name: projects[0].name })).toBeNull();
    expect(screen.getByText(projectsEn[0].highlights[0])).toBeInTheDocument();

    // 演示区：段卡片标题来自英文数据，两个 tablist 的 aria 也换了语言
    expect(screen.getByRole("tablist", { name: en.demo.platformTablistAria(projectsEn[0].name) })).toBeInTheDocument();
    expect(
      screen.getByRole("tablist", { name: en.demo.clipTablistAria(projectsEn[0].demos[0].label) }),
    ).toBeInTheDocument();
    expect(screen.getByText(en.demo.heading)).toBeInTheDocument();

    // 页脚：标题 / 说明 / 复制按钮（三态文案也走字典）
    const footer = screen.getByRole("contentinfo");
    expect(within(footer).getByRole("heading", { name: en.footer.heading })).toBeInTheDocument();
    expect(within(footer).getByRole("button", { name: en.footer.copy })).toBeInTheDocument();

    // `<html lang>` / 标签页标题 / 落盘
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toBe(en.meta.title);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("en");

    // 总闸：正文（含 aria-label / title / alt）不得残留中日韩字符。
    // 唯一豁免 = 语言开关自己那个「中」（语言名不翻译，见 `data/strings.ts`）。
    expect(textAndLabels(container, group).join(" | ")).not.toMatch(CJK);
  });

  it("点回「中」：整页回中文，`<html lang>`、标题、落盘一起回退", () => {
    render(<App />);
    pickLang("EN");
    pickLang("中");

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(zh.hero.title);
    expect(screen.getByRole("heading", { level: 3, name: projects[0].name })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3, name: projectsEn[0].name })).toBeNull();

    expect(document.documentElement.lang).toBe("zh-CN");
    expect(document.title).toBe(zh.meta.title);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("zh");
  });

  it("英文下的既有不变式仍成立：页脚仍是唯一的邮件出口（恰好一个 mailto）", () => {
    render(<App />);
    pickLang("EN");

    const footer = screen.getByRole("contentinfo");
    expect(within(footer).getAllByRole("link", { name: new RegExp(contact.email) })).toHaveLength(1);
    expect(within(screen.getByRole("banner")).queryByRole("link", { name: /@/ })).toBeNull();
  });
});

describe("语言偏好持久化（localStorage）", () => {
  it("上次选过英文：首帧就是英文（不是先闪中文再跳）", () => {
    window.localStorage.setItem(STORAGE_KEY, "en");

    render(<App />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(en.hero.title);
    expect(document.documentElement.lang).toBe("en");
  });

  it("localStorage 里是垃圾值时按默认中文渲染（不拿垃圾值去查字典、页面不空白）", () => {
    window.localStorage.setItem(STORAGE_KEY, "ja");

    render(<App />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(zh.hero.title);
    expect(document.documentElement.lang).toBe("zh-CN");
  });
});
