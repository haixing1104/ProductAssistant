// 作品集的数据契约。
//
// 设计口径：**内容是数据，不是代码** —— 加一个项目 = 在 `data/projects.ts` 追加一个对象，
// 页面（卡片 / 演示切换 / 联系方式）自动渲染，不需要改任何组件。
// 校验口径：`__tests__/projects.test.ts` 会检查这份数据的完整性，其中最关键的一条是
// **「声明了视频/首帧图就必须真实存在」** —— 文件名写错会在 `npm test` 当场变红，
// 而不是上线后变成一个白块（这类错在静态页上没人会及时发现）。

/**
 * 演示的三个端（Tab 顺序 = `data/projects.ts` 里 demos 的顺序）。
 *
 * 为什么原生端只有一条 `rn` 而不是 Android / iOS 各一条：原生端是**一套代码两端**，
 * 拆成两个 Tab 只会让 iOS 那条长期停在占位态（录制要 macOS / Xcode 或 iPhone 真机），
 * 访客看到的是"两个长得一样的端、其中一个永远没内容"。合并后一个 Tab 覆盖两端，
 * 素材按 `demos/<slug>/rn.<ext>` 放一段即可。
 */
export type Platform = "web" | "h5" | "rn";

/**
 * 一个端里的一段演示（一个动图或一段视频）。
 *
 * 为什么是「每端多段」而不是「一端一段」：**GIF 不能暂停、不能拖进度**，把完整流程压成一段，
 * 访客实际只会看到开头几秒就划走了。拆段后每段 20–35s、卡片上标出时长，访客按需点开，
 * 每次只下载当前这一段的字节（未选中的段零请求）。
 *
 * 三种状态（页面按「有 video → 有 gif → 只有 note」的优先级降级渲染）：
 *   1. 录好了        : `video`（推荐 mp4/webm）+ `poster`（首帧图，防布局跳动）
 *   2. 只有动图      : `gif`（体积是同内容 mp4 的 5–10 倍，非必要不用）
 *   3. 还没录        : 不写这一段，整端只留 `DemoAsset.note` → 渲染一块设计好的占位卡
 *
 * 路径相对 `public/`（如 `demos/pa/web/01-list.gif`），用 `lib/asset.ts` 的 `demoPath()` 生成，
 * 避免手写字符串拼错（拼错会被 `__tests__/projects.test.ts` 当场指出）。
 */
export type DemoClip = {
  /** 文件名主干（不含扩展名）：`demos/<slug>/<platform>/<key>.<ext>`。同时是段级 Tab 的 id 后缀。 */
  key: string;
  /** 分段卡片上的短标题，如「商品列表」。 */
  title: string;
  /** 时长徽标，如 `"25s"`。GIF 没有进度条，先告诉访客这段要看多久。 */
  duration: string;
  /** 这一段演示了什么（显示在舞台下方的一行图注）。 */
  note?: string;
  poster?: string;
  video?: string;
  gif?: string;
};

/**
 * 一个端的演示资产：**若干段** + 舞台比例。
 *
 * `clips` 为空数组 = 这一端还没录，页面用 `note` 渲染占位卡。
 * `aspect` 直接写 CSS 值（如 `"1882 / 912"`），与素材真实尺寸一致 —— 外框按素材比例走，
 * 既不裁掉界面文字，也不留黑边（16:9 硬框会把竖屏手机截图压成"矮胖"）。
 */
export type DemoAsset = {
  platform: Platform;
  /** Tab 上的名字，如「PC Web」/「Mobile Native (Android & iOS)」。 */
  label: string;
  /** 舞台外框比例（CSS `aspect-ratio` 值），与素材真实像素尺寸一致。 */
  aspect: string;
  /** 整端还没录时的说明（`clips` 为空时显示）。 */
  note?: string;
  clips: DemoClip[];
};

export type ProjectLinks = {
  /**
   * **PC Web 入口基址**（登录页 = 基址 + `/login`）。为空即禁用态（占位按钮 + 邮件联系）。
   *
   * 只存基址、不存完整地址：登录路径三端一致（`frontend/src/main.tsx` 与
   * `mobile-h5/src/main.tsx` 都是 `/login`），由 `lib/entry.ts` 的 `entryUrl()` 拼一次 ——
   * 就不会出现"某处写了 `/login`、另一处忘了写"的分叉。
   *
   * dev 的 `http://localhost:5173` **不写在这里**：下面的门禁（`__tests__/projects.test.ts`）
   * 要求 https，dev 地址走 `portfolio/.env.development` 的 `VITE_ENTRY_BASE_URL`。
   */
  live?: string;
  /** **Mobile H5 入口基址**（登录页 = 基址 + `/login`）；不填回退 `live`（同一套系统、两个入口）。 */
  liveH5?: string;
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
