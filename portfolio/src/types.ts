// 作品集的数据契约。
//
// 设计口径：**内容是数据，不是代码** —— 加一个项目 = 在 `data/projects.ts` 追加一个对象，
// 页面（卡片 / 演示切换 / 联系方式）自动渲染，不需要改任何组件。
// 校验口径：`__tests__/projects.test.ts` 会检查这份数据的完整性，其中最关键的一条是
// **「声明了视频/首帧图就必须真实存在」** —— 文件名写错会在 `npm test` 当场变红，
// 而不是上线后变成一个白块（这类错在静态页上没人会及时发现）。

/** 演示的四个端。顺序即页面上的 Tab 顺序。 */
export type Platform = "web" | "h5" | "rn-android" | "rn-ios";

/**
 * 一个端的演示资产。
 *
 * 三种状态（页面按「有 video → 有 gif → 只有 note」的优先级降级渲染）：
 *   1. 录好了        : `video`（推荐 mp4/webm）+ `poster`（首帧图，防布局跳动）
 *   2. 只有动图      : `gif`（体积是同内容 mp4 的 5–10 倍，非必要不用）
 *   3. 还没录        : 只写 `note`，页面渲染成一块**设计好的占位卡**（看起来是刻意留白，不是坏图）
 *
 * 路径相对 `public/`（如 `demos/pa/web.mp4`），用 `lib/asset.ts` 的 `demoPath()` 生成，
 * 避免手写字符串拼错。
 */
export type DemoAsset = {
  platform: Platform;
  /** Tab 上的名字，如「Web 工作台」。 */
  label: string;
  /** 还没录时的说明（录好后可以留着当图注）。 */
  note?: string;
  poster?: string;
  video?: string;
  gif?: string;
};

export type ProjectLinks = {
  /** 「进入系统」的地址。**为空即禁用态**（占位按钮 + 邮件联系），填上就自动变真链接。 */
  live?: string;
  repo?: string;
};

export type Project = {
  /** 同时是资产目录名：`public/demos/<slug>/`。 */
  slug: string;
  name: string;
  tagline: string;
  summary: string;
  period: string;
  status: "shipped" | "in-progress";
  stack: string[];
  highlights: string[];
  links: ProjectLinks;
  demos: DemoAsset[];
};
