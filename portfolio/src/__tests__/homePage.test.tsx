import { render, screen, within } from "@testing-library/react";

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

  it("四端演示 Tab 齐全，默认播/显示第一个端", () => {
    render(<App />);

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(firstProject.demos.length);
    expect(tabs.map((tab) => tab.textContent)).toEqual(firstProject.demos.map((demo) => demo.label));

    const [firstTab] = tabs;
    expect(firstTab).toHaveAttribute("aria-selected", "true");
    // 默认端还没有资产 → 渲染占位说明（文案来自数据里的 note）
    expect(screen.getByText(firstProject.demos[0].note!)).toBeInTheDocument();
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
