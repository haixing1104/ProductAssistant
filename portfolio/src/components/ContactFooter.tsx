import { useState } from "react";

import { contact, copyText, mailtoUrl } from "../lib/contact";

type CopyState = "idle" | "ok" | "fail";

// GitHub 标记：内联 SVG（本模块不引图标库，装饰一律内联 CSS / SVG —— 与 `EntryButton` 的箭头同款做法）。
// `aria-hidden`：可访问名由旁边的文字「GitHub」提供，图标不参与朗读。
const GITHUB_MARK = (
  <svg viewBox="0 0 16 16" aria-hidden className="size-4 shrink-0" fill="currentColor">
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
  </svg>
);

// 联系方式页脚：邮箱 + GitHub 两种联系方式。邮箱给三种给法 —— 点开邮件客户端（mailto）、
// 一键复制（复制失败时明确提示手动选择，而不是静默什么都不做）、以及 GitHub 源码仓库。
// 这里是全站**唯一**的联系方式出口：首屏与作品卡都不再重复放邮件入口。
export default function ContactFooter() {
  const [copyState, setCopyState] = useState<CopyState>("idle");

  async function handleCopy() {
    const ok = await copyText(contact.email);
    setCopyState(ok ? "ok" : "fail");
    // 2.5s 后复位成「复制邮箱」，让访客能再点一次
    window.setTimeout(() => setCopyState("idle"), 2500);
  }

  const copyLabel =
    copyState === "ok" ? "已复制 ✓" : copyState === "fail" ? "复制失败，请手动选中" : "复制邮箱";

  return (
    <footer id="contact" className="border-t border-ink-100 dark:border-white/10">
      <div className="mx-auto max-w-5xl px-6 py-16 sm:py-20">
        <h2 className="text-2xl font-semibold tracking-tight">联系我</h2>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-700 dark:text-ink-100/80">
          想聊项目细节、看完整代码，或者想申请演示环境的账号，都可以直接发邮件，或者点 GitHub 直接看源码。
        </p>

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <a className="btn-primary" href={mailtoUrl(contact.email, "来自作品集的联系")}>
            {contact.email}
          </a>
          <button type="button" className="btn-ghost" onClick={handleCopy}>
            {copyLabel}
          </button>
          {contact.github && (
            <a className="btn-ghost" href={contact.github} target="_blank" rel="noreferrer noopener">
              {GITHUB_MARK}
              GitHub
            </a>
          )}
        </div>

        <p className="mt-10 text-xs text-ink-500 dark:text-ink-100/50">
          © {new Date().getFullYear()} {contact.displayName} · 本站为纯静态页面
        </p>
      </div>
    </footer>
  );
}
