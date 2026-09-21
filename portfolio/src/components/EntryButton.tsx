import { entryUrl } from "../lib/entry";
import type { Platform, Project } from "../types";
import { useLang } from "./LanguageProvider";

type EntryButtonProps = {
  project: Project;
  /** 当前端（由作品卡持有）：决定入口指向哪套系统，或退化成 Toast。 */
  platform: Platform;
  /** 原生端没有浏览器入口 —— 点了交给父级弹 Toast。 */
  onUnsupported: () => void;
  /** 外部定位用（标题行右侧靠 `ms-auto` 推到最右）。 */
  className?: string;
};

// 作品卡标题行右侧的「开始使用」入口：**目标跟随当前端切换**。
//   · PC Web / Mobile H5 → 真链接，新窗口打开（访客看完还能回到宣传页）；
//   · Mobile Native      → 可点的按钮 + Toast（RN 是装在手机里的 App，浏览器里跳不过去）；
//   · 没配地址（生产未接入演示环境）→ 返回 null：**页面上绝不出现点了 404 的按钮**。
//
// 箭头是内联 SVG：本模块不引图标库（见 styles/app.css 的纪律），方向取 `→` 与底部
// 「进入系统 →」同一语义；hover 时箭头微移（`motion-reduce` 下不动）。
const ARROW = (
  <svg
    viewBox="0 0 16 16"
    aria-hidden
    className="size-3.5 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none"
  >
    <path
      d="M2 8h11M9.5 4.5 13 8l-3.5 3.5"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export default function EntryButton({ project, platform, onUnsupported, className }: EntryButtonProps) {
  const { t } = useLang();
  const label = project.demos.find((demo) => demo.platform === platform)?.label ?? platform;
  const classes = [
    "group inline-flex shrink-0 items-center gap-1.5 rounded-full border border-ink-200 px-4 py-2 text-sm font-medium text-ink-700 transition outline-none",
    "hover:bg-ink-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600",
    "dark:border-white/15 dark:text-ink-50 dark:hover:bg-white/5",
    className ?? "",
  ].join(" ");

  if (platform === "rn") {
    return (
      <button
        type="button"
        onClick={onUnsupported}
        title={t.card.disabledApp}
        aria-label={t.card.ariaStartApp(project.name)}
        className={classes}
      >
        {t.card.startUsingApp}
        {ARROW}
      </button>
    );
  }

  const url = entryUrl(platform, project.links);
  if (!url) return null;

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      aria-label={t.card.ariaStart(project.name, label)}
      className={classes}
    >
      {t.card.startUsing}
      {ARROW}
    </a>
  );
}
