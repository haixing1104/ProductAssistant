import { useState } from "react";

import { contact, copyText, mailtoUrl } from "../lib/contact";

type CopyState = "idle" | "ok" | "fail";

// 联系方式页脚：邮箱是唯一联系方式，因此给了三种给法 —— 点开邮件客户端（mailto）、
// 一键复制（复制失败时明确提示手动选择，而不是静默什么都不做）、以及可选的 GitHub。
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
          想聊项目细节、看完整代码，或者想申请演示环境的账号，都可以直接发邮件。
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
              GitHub
            </a>
          )}
        </div>

        <p className="mt-10 text-xs text-ink-500 dark:text-ink-100/50">
          © {new Date().getFullYear()} {contact.displayName} · 本站为纯静态页面（无后端、无追踪脚本）
        </p>
      </div>
    </footer>
  );
}
