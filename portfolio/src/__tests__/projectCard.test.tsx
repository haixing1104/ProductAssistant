import { act, fireEvent, render, screen, within } from "@testing-library/react";

import ProjectCard from "../components/ProjectCard";
import { demoPath } from "../lib/asset";
import { APP_DOWNLOAD_NOTE } from "../lib/entry";
import type { Project } from "../types";

// 作品卡**组件级**回归：主要守标题行右侧的「开始使用」入口（跟随当前端切目标）
// 与它带来的那次结构改动（`platform` 状态从 DemoSwitcher 提升到 ProjectCard）。
//
// 为什么构造作品对象而不是用 `data/projects.ts`：真实数据的 `links` 现在是空的
// （演示环境未接入 = 生产现状），没有入口可断言。构造一份带链接的，才能把
// 「跟随端切换 / 原生端退化成 Toast / 没链接就不渲染」钉死在组件上；
// 真实数据的占位态由 `homePage.test.tsx` 守着。

const project: Project = {
  slug: "pa",
  name: "ProductAssistant 产品上线助手",
  tagline: "一句话卖点",
  summary: "综述",
  period: "2025.09 – 至今",
  status: "shipped",
  stack: ["React 19"],
  highlights: ["要点一"],
  links: { live: "https://demo.example.com", liveH5: "https://m.example.com" },
  demos: [
    {
      platform: "web",
      label: "PC Web",
      aspect: "1882 / 912",
      clips: [
        { key: "01-list", title: "商品列表", duration: "25s", gif: demoPath("pa", "web", "01-list", "gif") },
        {
          key: "02-generate",
          title: "AI 生成",
          duration: "153s",
          gif: demoPath("pa", "web", "02-generate", "gif"),
        },
      ],
    },
    {
      platform: "h5",
      label: "Mobile H5",
      aspect: "493 / 854",
      clips: [
        {
          key: "01-generate",
          title: "AI 生成",
          duration: "87s",
          gif: demoPath("pa", "h5", "01-generate", "gif"),
        },
      ],
    },
    {
      platform: "rn",
      label: "Mobile Native (Android & iOS)",
      aspect: "240 / 520",
      clips: [
        {
          key: "01-overview",
          title: "原生 App 全链路",
          duration: "30s",
          gif: demoPath("pa", "rn", "01-overview", "gif"),
        },
      ],
    },
  ],
};

/** 标题行入口（跟随端，轻量 pill） */
const entryLink = () => screen.queryByRole("link", { name: /开始使用/ });
/** 底部主 CTA（与标题行同源，看完演示后的决策点） */
const bottomLink = () => screen.queryByRole("link", { name: /进入系统/ });
const platformTab = (name: string) => screen.getByRole("tab", { name });
const stage = () => screen.getByRole("tabpanel");
const stageSrc = () => within(stage()).getByRole("img").getAttribute("src");

describe("作品卡：「开始使用」入口跟随当前端", () => {
  it("默认端（PC Web）是真链接：基址 + /login，新窗口打开（访客不丢宣传页）", () => {
    render(<ProjectCard project={project} />);

    const link = screen.getByRole("link", { name: /开始使用/ });
    expect(link).toHaveAttribute("href", "https://demo.example.com/login");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noreferrer"));

    // 底部主 CTA 与标题行入口**同源**（单一事实源）—— href 必须恒等。
    // 这条断言就是"两处逻辑一致"的机器化表述：谁把底部再写死成 web，它立刻变红。
    const bottom = screen.getByRole("link", { name: /进入系统/ });
    expect(bottom.getAttribute("href")).toBe(link.getAttribute("href"));
    expect(bottom).toHaveAttribute("target", "_blank");
  });

  it("切到 Mobile H5：标题行与底部一起换成 H5 的登录地址（不是多出一个入口）", () => {
    render(<ProjectCard project={project} />);

    fireEvent.click(platformTab("Mobile H5"));

    expect(screen.getAllByRole("link", { name: /开始使用/ })).toHaveLength(1);
    expect(entryLink()).toHaveAttribute("href", "https://m.example.com/login");
    // 底部同样跟随 —— 这里钉住"底部不再固定 PC Web"
    expect(bottomLink()).toHaveAttribute("href", "https://m.example.com/login");
    expect(bottomLink()?.getAttribute("href")).toBe(entryLink()?.getAttribute("href"));
  });

  it("切回 PC Web：两处链接又一起回来（入口不残留上一端的状态）", () => {
    render(<ProjectCard project={project} />);

    fireEvent.click(platformTab("Mobile H5"));
    fireEvent.click(platformTab("PC Web"));

    expect(entryLink()).toHaveAttribute("href", "https://demo.example.com/login");
    expect(bottomLink()).toHaveAttribute("href", "https://demo.example.com/login");
  });

  it("原生端：入口变成按钮（浏览器里跳不过去），点击弹 Toast 并说清替代路径，几秒后自动消失", () => {
    vi.useFakeTimers();
    try {
      render(<ProjectCard project={project} />);
      fireEvent.click(platformTab("Mobile Native (Android & iOS)"));

      // 标题行不给链接（点了要能解释，否则访客以为坏了）；
      // 底部不给死链 —— 换成禁用态 + 一行原因（比"点了才弹 Toast"更早说清）
      expect(entryLink()).toBeNull();
      expect(bottomLink()).toBeNull();
      expect(screen.getByRole("button", { name: "App 下载暂未开放" })).toBeDisabled();
      expect(screen.getByText(/尚未上架应用商店/)).toBeTruthy();
      expect(screen.queryByRole("status")).toBeNull();
      // 联系方式统一收在页脚：卡片上不再补一个「邮件联系我试用」入口
      expect(screen.queryByRole("link", { name: /邮件/ })).toBeNull();
      const button = screen.getByRole("button", { name: /原生 App/ });

      fireEvent.click(button);
      expect(screen.getByRole("status")).toHaveTextContent(APP_DOWNLOAD_NOTE);

      act(() => {
        vi.advanceTimersByTime(3200);
      });
      expect(screen.queryByRole("status")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("作品卡：没有入口地址时（生产现状）", () => {
  it("标题行不渲染入口；底部是禁用占位（邮件入口只在页脚，卡片不再重复）", () => {
    render(<ProjectCard project={{ ...project, links: {} }} />);

    expect(entryLink()).toBeNull();
    expect(screen.getByRole("button", { name: "演示环境准备中" })).toBeDisabled();
    expect(screen.queryByRole("link", { name: /进入系统/ })).toBeNull();
    // 唯一的行动点交给页脚：卡片上既不给死链，也不重复给邮件入口
    expect(screen.queryByRole("link", { name: /邮件/ })).toBeNull();
  });
});

describe("作品卡：换端后段选择重置（platform 提升到父级后的回归）", () => {
  it("PC Web 选第 2 段 → 切 H5 → 切回 PC Web：回到第 1 段（不同端同名 key 也不串台）", () => {
    render(<ProjectCard project={project} />);
    const clipTabs = () => within(screen.getByRole("tablist", { name: /演示分段/ })).getAllByRole("tab");

    expect(stageSrc()).toContain("demos/pa/web/01-list.gif");

    fireEvent.click(clipTabs()[1]);
    expect(stageSrc()).toContain("demos/pa/web/02-generate.gif");

    fireEvent.click(platformTab("Mobile H5"));
    expect(stageSrc()).toContain("demos/pa/h5/01-generate.gif");

    fireEvent.click(platformTab("PC Web"));
    expect(stageSrc()).toContain("demos/pa/web/01-list.gif");
  });
});
