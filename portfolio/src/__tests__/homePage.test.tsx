import { fireEvent, render, screen, within } from "@testing-library/react";

import App from "../App";
import { projects } from "../data/projects";
import { contact, copyText, mailtoUrl } from "../lib/contact";

// 页面级冒烟：挡住「数据加了一条 → 页面崩了」「类名/字段改名忘了同步」这类事故。
// 不追求渲染细节与样式断言（原子类的可见效果靠浏览器真机验收）。
describe("作品集首页", () => {
  const firstProject = projects[0];

  it("首屏与作品名都渲染出来了", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: firstProject.name })).toBeInTheDocument();
    expect(screen.getByText(firstProject.tagline)).toBeInTheDocument();
  });

  it("联系区有三种联系途径：发邮件 / 一键复制 / GitHub 源码仓库", () => {
    render(<App />);

    const footer = screen.getByRole("contentinfo");
    const mailLink = within(footer).getByRole("link", { name: contact.email });
    expect(mailLink).toHaveAttribute("href", expect.stringContaining(`mailto:${contact.email}`));
    expect(within(footer).getByRole("button", { name: "复制邮箱" })).toBeInTheDocument();

    // GitHub 入口：新窗口打开 + `rel` 带 noreferrer（不把来源页交给对方）
    const githubLink = within(footer).getByRole("link", { name: "GitHub" });
    expect(githubLink).toHaveAttribute("href", contact.github);
    expect(githubLink).toHaveAttribute("target", "_blank");
    expect(githubLink).toHaveAttribute("rel", expect.stringContaining("noreferrer"));
  });

  // 首屏关于「联系」什么都不放：邮件 / GitHub 一律只在页脚 —— 同一个动作在页面里只出现一次。
  // 这条负向断言钉住的就是「去重复」本身：谁把邮件按钮加回首屏，它立刻变红。
  it("首屏不留联系方式（全站唯一出口是页脚）", () => {
    render(<App />);

    const hero = screen.getByRole("banner");
    expect(within(hero).queryByRole("link", { name: /邮件|GitHub/ })).toBeNull();

    // 全页 mailto 链接恰好一个 = 页脚那个（正文里的邮箱文本不会再变成第二个入口）
    expect(screen.getAllByRole("link", { name: new RegExp(contact.email) })).toHaveLength(1);
  });

  // 演示环境**已接入**（`links.live` / `links.liveH5` 已填生产域名）之后的状态契约：
  //   · 底部「进入系统 →」与标题行「开始使用」都必须是**真链接**（占位态必须消失，
  //     否则页面上会同时出现禁用的死按钮和真链接）；
  //   · 两者**同源**：同一个 href（标题行是入口、底部是主 CTA，指向同一地址）；
  //   · 必须是 https —— 与 __tests__/projects.test.ts 的基址门禁互为正反面：
  //     那边管"数据格式"，这里管"页面真的用上了数据"。
  //   · 原生端（rn Tab）例外：没有可跳的 URL → 底部是禁用占位、标题行是 Toast 按钮，
  //     那部分契约由 __tests__/projectCard.test.tsx 用构造数据守护。
  //
  // 注意：vitest 的 mode 是 `test`（不读 .env.development），所以这里看到的就是**生产**形态。
  it("演示环境已接入：两个入口都是 https 真链接，且底部与标题行同源", () => {
    render(<App />);

    const enterLink = screen.getByRole("link", { name: /进入系统/ });
    const startLink = screen.getByRole("link", { name: /开始使用/ });

    expect(enterLink).toHaveAttribute("href", expect.stringMatching(/^https:\/\//));
    expect(enterLink.getAttribute("href")).toBe(startLink.getAttribute("href"));

    // 占位态必须消失（已接入就不该再出现）；卡片上的「邮件联系我试用」兜底入口也已移除 ——
    // 联系方式全站只在页脚，页面上不该再出现第二个「发邮件」的入口。
    expect(screen.queryByRole("button", { name: "演示环境准备中" })).toBeNull();
  });

});

// 演示区：端 Tab（3 个）+ 端内分段卡片 + 舞台。
// 这一组用例把「每端多段」这个需求钉死在页面上，而不只是钉在数据里。
describe("三端演示（端 Tab + 分段 + 舞台）", () => {
  const firstProject = projects[0];
  const webDemo = firstProject.demos[0];
  const rnDemo = firstProject.demos[firstProject.demos.length - 1];

  const platformTabs = () => within(screen.getByRole("tablist", { name: /多端演示/ })).getAllByRole("tab");
  const clipTabs = () => within(screen.getByRole("tablist", { name: /演示分段/ })).getAllByRole("tab");
  const stage = () => screen.getByRole("tabpanel");

  it("端 Tab 齐全（PC Web / Mobile H5 / Mobile Native），默认显示第一个端", () => {
    render(<App />);

    // 需求钉死：恰好三个端、标签就是这三个 —— 故意不复用数据，
    // 谁再加/减一个端（或改标签）都该在这里被发现，而不是悄悄上线。
    expect(platformTabs().map((tab) => tab.textContent)).toEqual([
      "PC Web",
      "Mobile H5",
      "Mobile Native (Android & iOS)",
    ]);
    // 再与数据对齐一次：字面量与 `data/projects.ts` 任一侧改了、另一侧没跟上都会红。
    expect(platformTabs().map((tab) => tab.textContent)).toEqual(firstProject.demos.map((demo) => demo.label));

    const [firstTab] = platformTabs();
    expect(firstTab).toHaveAttribute("aria-selected", "true");
  });

  it("默认端渲染出它的全部分段卡片：标题 + 时长徽标，默认选中第一段", () => {
    render(<App />);

    // 段数 = 数据里的段数（PC Web 4 段）；时长必须显示 —— GIF 不能暂停，访客先知道要看多久
    expect(clipTabs()).toHaveLength(webDemo.clips.length);
    expect(clipTabs().map((tab) => tab.textContent)).toEqual(
      webDemo.clips.map((clip) => `${clip.title}${clip.duration}`),
    );
    expect(clipTabs()[0]).toHaveAttribute("aria-selected", "true");
  });

  it("舞台上只挂当前那一段：未选中的段零请求（不是渲染出来再隐藏）", () => {
    render(<App />);

    const images = within(stage()).getAllByRole("img");
    expect(images).toHaveLength(1);
    expect(images[0].getAttribute("src")).toContain(webDemo.clips[0].gif!);
    expect(within(stage()).queryByRole("video")).toBeNull();
  });

  it("点分段卡片换舞台内容，卡片选中态跟着走", () => {
    render(<App />);

    const second = clipTabs()[1];
    fireEvent.click(second);

    expect(second).toHaveAttribute("aria-selected", "true");
    expect(clipTabs()[0]).toHaveAttribute("aria-selected", "false");
    expect(within(stage()).getByRole("img").getAttribute("src")).toContain(webDemo.clips[1].gif!);
  });

  it("←/→ 也能切段（键盘可达），焦点跟着移到新选中的卡片", () => {
    render(<App />);

    fireEvent.keyDown(screen.getByRole("tablist", { name: /演示分段/ }), { key: "ArrowRight" });
    expect(clipTabs()[1]).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(clipTabs()[1]);

    // 到头回绕：再按 ← 回到前一段
    fireEvent.keyDown(screen.getByRole("tablist", { name: /演示分段/ }), { key: "ArrowLeft" });
    expect(clipTabs()[0]).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(clipTabs()[0]);
  });

  it("只有一段的端不渲染分段卡片（不给「只有一个选项的选择器」）", () => {
    render(<App />);

    fireEvent.click(screen.getByRole("tab", { name: "Mobile Native (Android & iOS)" }));

    expect(rnDemo.clips).toHaveLength(1);
    expect(screen.queryByRole("tablist", { name: /演示分段/ })).toBeNull();
    expect(within(stage()).getByRole("img").getAttribute("src")).toContain(rnDemo.clips[0].gif!);
  });

  it("换端后段选择重置为第一段（不同端出现同名段 key 也不会串台）", () => {
    render(<App />);

    fireEvent.click(clipTabs()[1]);
    expect(within(stage()).getByRole("img").getAttribute("src")).toContain(webDemo.clips[1].gif!);

    fireEvent.click(screen.getByRole("tab", { name: "Mobile H5" }));
    const h5Demo = firstProject.demos[1];
    expect(within(stage()).getByRole("img").getAttribute("src")).toContain(h5Demo.clips[0].gif!);

    // 切回 Web 端：回到第一段，而不是"记住"刚才的第二段
    fireEvent.click(screen.getByRole("tab", { name: "PC Web" }));
    expect(within(stage()).getByRole("img").getAttribute("src")).toContain(webDemo.clips[0].gif!);
  });
});

describe("联系方式工具函数", () => {
  it("mailtoUrl：不传参数时不带 ?，传了就正确编码（空格编成 %20 而不是 +）", () => {
    expect(mailtoUrl(contact.email)).toBe(`mailto:${contact.email}`);
    expect(mailtoUrl(contact.email, "你好 世界")).toBe(`mailto:${contact.email}?subject=%E4%BD%A0%E5%A5%BD%20%E4%B8%96%E7%95%8C`);
  });

  it("copyText：Clipboard API 可用时走它", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

    await expect(copyText(contact.email)).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith(contact.email);
  });

  it("copyText：Clipboard 不可用（http 页面）时退回 execCommand，仍能复制成功", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });

    await expect(copyText(contact.email)).resolves.toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("copyText：两条路都失败时返回 false（调用方据此提示手动复制）", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => {
        throw new Error("not supported");
      }),
      configurable: true,
    });

    await expect(copyText(contact.email)).resolves.toBe(false);
  });
});

// 「声明即校验」：联系方式本身也是数据 —— 写错了页面不会报错，
// 只会静默少一个入口（`undefined`）或把访客送到 404（错地址）。
describe("联系方式数据（src/lib/contact.ts）", () => {
  it("署名与邮箱填齐；GitHub 若填，必须是 https 的公开仓库地址且不带尾斜杠", () => {
    expect(contact.displayName.trim()).not.toBe("");
    expect(contact.email).toMatch(/^[^\s@]+@[^\s@]+\.[^\s@]+$/);

    if (!contact.github) return; // 未填 = 页脚不渲染该入口（见 ContactFooter 的条件渲染）
    expect(contact.github, "只填公开仓库：私有仓库访客点开是 404").toMatch(
      /^https:\/\/github\.com\/[\w.-]+(\/[\w.-]+)?$/,
    );
    expect(contact.github.endsWith("/")).toBe(false);
  });
});
