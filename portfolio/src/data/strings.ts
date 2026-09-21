import type { Lang } from "../types";

// =============================================================================
// UI 文案字典（**唯一文案事实源**，中文为基准、英文为对照）
//
// 纪律（与 `lib/contact.ts` 是联系方式唯一事实源同款）：
//   · 组件里**不许硬编码面向访客的文案**——一律从 `useLang().t` 取（含 aria-label / title / alt，
//     屏幕阅读器的朗读语言也要跟着界面语言走）；
//   · 带参数的文案（aria-label、版权行）写成**函数**而不是 `{title} 演示` 这种模板字符串：
//     模板只写一次，翻译时不会漏改占位符，参数类型也由 tsc 检查。
//
// 结构对齐由**类型**保证：`const en: typeof zh` —— 漏译一个 key、多写一个 key、
// 数组长度不同、函数签名不一致，`tsc --noEmit` 当场报错。因此运行时不需要任何
// 「缺 key 就回退中文」的兜底逻辑（那类兜底只会把漏译藏到线上）。
// 语言名本身不翻译（任何语言下都显示 `中` / `EN`），所以 `langSwitch.zh` 在英文包里
// **故意留中文** —— `__tests__/strings.test.ts` 的「英文包不得残留中文」门禁对此显式豁免。
// =============================================================================

const zh = {
  /** 语言开关自身（`zh` = 中文按钮上的字，`en` = 英文按钮上的字；语言名不随界面语言翻译）。 */
  langSwitch: {
    group: "切换界面语言",
    zh: "中",
    en: "EN",
  },

  /** 切语言时同步的 `<title>`（`index.html` 里那份是构建期中文，见该文件注释）。 */
  meta: {
    title: "作品集 · ProductAssistant 产品上线助手",
  },

  hero: {
    eyebrow: "Portfolio · 作品集",
    title: "全栈工程 · 多端交付",
    intro:
      "把一条业务，从数据库做到三端界面：后端网关、AI 编排（人工审批介入）、桌面工作台、移动端 H5 与原生 App，全链路落地。",
    /** 首屏唯一的动作（联系入口一律收在页脚）。 */
    cta: "查看作品 ↓",
    /** 三条事实：label + hint，顺序即展示顺序。 */
    facts: [
      { label: "3 端界面", hint: "桌面 / H5 / 原生 App" },
      { label: "1 套契约层", hint: "三端共用 api + store" },
      { label: "2 层服务隔离", hint: "业务网关 / AI 引擎" },
    ],
  },

  card: {
    /** 作品状态徽标（键是数据里的枚举，值是文案）。 */
    status: { shipped: "已上线", "in-progress": "开发中" },
    startUsing: "开始使用",
    startUsingApp: "开始使用 App",
    enterSystem: "进入系统 →",
    repo: "源码仓库",
    /** 原生端（App 未上架）的禁用态。 */
    disabledApp: "App 下载暂未开放",
    /** 未接入演示环境时的禁用态。 */
    disabledEntry: "演示环境准备中",
    /** 原生端禁用态下方的一行原因（比「点了才知道」更早说清）。 */
    noteApp: "App 尚未上架应用商店；上面的端 Tab 可切到 Mobile H5 / PC Web，在浏览器里直接体验。",
    /** 未接入演示环境时的一行原因。 */
    noteNotConnected: "演示环境（域名 + 只读演示账号 + 成本护栏）尚在部署中；接入后此处会变成可直接操作的入口。",
    /** 原生端点了「开始使用 App」的 Toast（原 `lib/entry.ts` 的 `APP_DOWNLOAD_NOTE`：它是文案，不是 URL 逻辑）。 */
    toastAppDownload:
      "App 下载暂未开放（Android / iOS 安装包尚未上架）；可先切到 Mobile H5 在浏览器里体验。",
    ariaStart: (name: string, label: string) => `开始使用：打开 ${name} 的 ${label} 登录页`,
    ariaStartApp: (name: string) => `开始使用：${name} 原生 App（当前不支持下载）`,
    /** 底部主 CTA 的可访问名：**必须与标题行的「开始使用」区分开**（否则屏幕阅读器读到两个同名入口）。 */
    ariaEnterSystem: (name: string, label: string) => `进入系统：打开 ${name} 的 ${label} 登录页`,
  },

  demo: {
    heading: "多端演示",
    platformTablistAria: (name: string) => `${name} 多端演示`,
    clipTablistAria: (label: string) => `${label} 演示分段`,
    /** 演示图的 alt（jsdom 不加载图片，alt 是访客在图片加载失败时唯一能看到的东西）。 */
    clipAlt: (title: string) => `${title} 演示`,
    /**
     * 段说明里「标题 → 说明」之间的分隔符。**标点也是文案**：中文用全角冒号，
     * 英文用半角冒号 + 空格 —— 写死在 JSX 里的话，英文页面上会冒出一个全角「：」
     * （`__tests__/strings.test.ts` 的中文残留门禁正是抓这个）。
     */
    clipNoteSeparator: "：",
    placeholderHint: "资产就位后此处自动替换为演示动图 / 视频",
  },

  footer: {
    heading: "联系我",
    intro: "想聊项目细节、看完整代码，或者想申请演示环境的账号，都可以直接发邮件，或者点 GitHub 直接看源码。",
    copy: "复制邮箱",
    copied: "已复制 ✓",
    copyFailed: "复制失败，请手动选中",
    /** mailto 的默认主题。 */
    mailSubject: "来自作品集的联系",
    copyright: (year: number, name: string) => `© ${year} ${name} · 本站为纯静态页面`,
  },
};

/**
 * 英文文案。类型标注就是**对齐门禁**：任何一处与中文包不一致（key / 值类型 / 数组长度 /
 * 函数参数个数）都会让 `npx tsc --noEmit` 报错，而不是留到线上少一句话。
 */
const en: typeof zh = {
  langSwitch: {
    group: "Switch interface language",
    // 语言名不翻译：任何界面语言下，这个按钮都写着「中」（见文件头的豁免说明）
    zh: "中",
    en: "EN",
  },

  meta: {
    title: "Portfolio · ProductAssistant, an AI copilot for product listings",
  },

  hero: {
    eyebrow: "Portfolio",
    title: "Full-stack engineering · multi-platform delivery",
    intro:
      "One business carried from the database all the way to three clients: a business gateway, AI orchestration with human approval in the loop, a desktop console, a mobile H5 app and a native app.",
    cta: "See the work ↓",
    facts: [
      { label: "3 client apps", hint: "Desktop / H5 / native" },
      { label: "1 shared contract core", hint: "api + store across all three" },
      { label: "2 isolated service layers", hint: "business gateway / AI engine" },
    ],
  },

  card: {
    status: { shipped: "Shipped", "in-progress": "In progress" },
    startUsing: "Open the app",
    startUsingApp: "Get the app",
    enterSystem: "Enter the system →",
    repo: "Source repo",
    disabledApp: "App download not available yet",
    disabledEntry: "Demo environment coming soon",
    noteApp:
      "The app is not on any app store yet; switch the platform tabs above to Mobile H5 or PC Web and try it right in the browser.",
    noteNotConnected:
      "The demo environment (domain + read-only demo account + cost guardrails) is still being deployed; once it is live, this becomes a directly usable entry point.",
    toastAppDownload:
      "App download is not available yet (no Android / iOS build has been published); switch to Mobile H5 to try it in the browser.",
    ariaStart: (name: string, label: string) => `Open the ${label} login page of ${name}`,
    ariaStartApp: (name: string) => `Get the ${name} native app (download not available yet)`,
    ariaEnterSystem: (name: string, label: string) => `Enter the system: open the ${label} login page of ${name}`,
  },

  demo: {
    heading: "Multi-platform demos",
    platformTablistAria: (name: string) => `${name} multi-platform demos`,
    clipTablistAria: (label: string) => `${label} demo clips`,
    clipAlt: (title: string) => `${title} demo`,
    clipNoteSeparator: ": ",
    placeholderHint: "A GIF or video will replace this placeholder once the asset is ready",
  },

  footer: {
    heading: "Get in touch",
    intro:
      "Happy to walk through the details, share the full source, or hand out a demo account — email me directly, or open the GitHub repo and read the code.",
    copy: "Copy email",
    copied: "Copied ✓",
    copyFailed: "Copy failed — please select it manually",
    mailSubject: "Contact from the portfolio site",
    copyright: (year: number, name: string) => `© ${year} ${name} · Static site, no backend`,
  },
};

/** 一种语言对应一份完整字典（两种语言结构完全相同，见上面的 `en: typeof zh`）。 */
export type Strings = typeof zh;

/** 语言 → 字典。组件一律通过 `useLang().t` 取，不要直接 import 这个表。 */
export const STRINGS: Record<Lang, Strings> = { zh, en };
