import { fireEvent, render, screen, within } from "@testing-library/react";

import App from "../App";
import { projects } from "../data/projects";
import { contact, copyText, mailtoUrl } from "../lib/contact";

// 页面级冒烟：挡住「数据加了一条 → 页面崩了」「类名/字段改名忘了同步」这类事故。
// 不追求渲染细节与样式断言（原子类的可见效果靠浏览器验收，见 portfolio/README.md 的验收清单）。
describe("作品集首页", () => {
  const firstProject = projects[0];

  it("首屏与作品名都渲染出来了", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: firstProject.name })).toBeInTheDocument();
    expect(screen.getByText(firstProject.tagline)).toBeInTheDocument();
  });

  it("联系区有邮箱：既能点开发邮件，也能一键复制", () => {
    render(<App />);

    const footer = screen.getByRole("contentinfo");
    const mailLink = within(footer).getByRole("link", { name: contact.email });
    expect(mailLink).toHaveAttribute("href", expect.stringContaining(`mailto:${contact.email}`));
    expect(within(footer).getByRole("button", { name: "复制邮箱" })).toBeInTheDocument();
  });

  // 这条用例就是「演示环境未接入」这个状态的契约：将来 `links.live` 填上之后，
  // 它会失败并提醒你一起把占位态的断言改掉（否则页面上会同时出现死按钮和真链接）。
  it("演示环境未接入时，「进入系统」是禁用占位态，而不是一个死链", () => {
    render(<App />);

    expect(screen.getByRole("button", { name: "演示环境准备中" })).toBeDisabled();
    expect(screen.queryByRole("link", { name: /进入系统/ })).toBeNull();
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
