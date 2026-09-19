import { useEffect, useState } from "react";

import { contact, mailtoUrl } from "../lib/contact";
import { APP_DOWNLOAD_NOTE, entryUrl } from "../lib/entry";
import type { Platform, Project } from "../types";
import DemoSwitcher from "./DemoSwitcher";
import EntryButton from "./EntryButton";
import Toast from "./Toast";

const STATUS_TEXT: Record<Project["status"], string> = {
  shipped: "已上线",
  "in-progress": "开发中",
};

/** Toast 停留时长（与 `ContactFooter` 的"复制成功"复位同为几秒级，不用通知库）。 */
const TOAST_MS = 3200;

// 作品卡：名称/状态（+「开始使用」入口）→ 一句话卖点 → 综述 → 要点 → 技术栈 → 进入系统 → 三端演示。
//
// **本卡持有「当前端」状态**（不再是 DemoSwitcher 内部状态）：标题行右侧的「开始使用」
// 入口要跟着当前端切目标（PC Web :5173 / Mobile H5 :5174 / 原生 Toast），
// 因此它必须和端 Tab 共享同一份状态 —— 提升到最近的公共父级是唯一不出现"两处各记一份、
// 迟早不同步"的做法。`DemoSwitcher` 相应变成受控组件（见该文件）。
//
// 「进入系统」的三态仍然有效（也是将来接演示环境的唯一改动点）：
//   · 入口基址有值（`links.live` 或 dev 的 `VITE_ENTRY_BASE_URL`）→ 真链接，新窗口打开；
//   · 没有值（生产尚未接入）→ **禁用态按钮 + 邮件联系**，并明确写出「为什么还不能进」。
// 这就是为什么演示环境还没落地也能先把页面放出去：访客不会点进一个 404。
export default function ProjectCard({ project }: { project: Project }) {
  const [platform, setPlatform] = useState<Platform | undefined>(project.demos[0]?.platform);
  const [toast, setToast] = useState<string | null>(null);
  // 入口地址只解析一次（web 端），标题行的入口自己按当前端解析
  const webEntry = entryUrl("web", project.links);

  // 定时器写在 effect 里（而不是裸 setTimeout）：卸载时清掉，避免组件消失后 setState
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [toast]);

  return (
    <article className="card">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-xl font-semibold tracking-tight">{project.name}</h3>
        <span className="chip text-brand-600 dark:text-brand-500">{STATUS_TEXT[project.status]}</span>
        <span className="text-sm text-ink-500 dark:text-ink-100/60">{project.period}</span>
        {/* 标题行右侧的入口：跟随当前端；没配地址时组件自身返回 null（不留空位、不给死链） */}
        {platform && (
          <EntryButton
            project={project}
            platform={platform}
            onUnsupported={() => setToast(APP_DOWNLOAD_NOTE)}
            className="ms-auto"
          />
        )}
      </div>

      <p className="mt-3 text-base font-medium text-ink-900 dark:text-ink-50">{project.tagline}</p>
      <p className="mt-3 text-sm leading-relaxed text-ink-700 dark:text-ink-100/80">{project.summary}</p>

      <ul className="mt-6 space-y-2">
        {project.highlights.map((highlight) => (
          <li key={highlight} className="flex gap-3 text-sm leading-relaxed text-ink-700 dark:text-ink-100/80">
            {/* 用圆点而不是默认 list-style：Preflight 已清掉 ul 的项目符号，这里显式画一个更可控 */}
            <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-brand-500" />
            <span>{highlight}</span>
          </li>
        ))}
      </ul>

      <div className="mt-6 flex flex-wrap gap-2">
        {project.stack.map((item) => (
          <span key={item} className="chip">
            {item}
          </span>
        ))}
      </div>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        {webEntry ? (
          <a className="btn-primary" href={webEntry} target="_blank" rel="noreferrer noopener">
            进入系统 →
          </a>
        ) : (
          <>
            <button type="button" className="btn-disabled" disabled aria-disabled="true">
              演示环境准备中
            </button>
            <a className="btn-ghost" href={mailtoUrl(contact.email, `${project.name} 试用申请`)}>
              邮件联系我试用
            </a>
          </>
        )}
        {project.links.repo && (
          <a className="btn-ghost" href={project.links.repo} target="_blank" rel="noreferrer noopener">
            源码仓库
          </a>
        )}
      </div>

      {!webEntry && (
        <p className="mt-3 text-xs text-ink-500 dark:text-ink-100/60">
          演示环境（域名 + 只读演示账号 + 成本护栏）尚在部署中；接入后此处会变成可直接操作的入口。
        </p>
      )}

      <DemoSwitcher project={project} platform={platform} onPlatformChange={setPlatform} />

      {/* 唯一的 Toast 用途：原生端点了「开始使用 App」—— 解释为什么跳不过去，并给替代路径 */}
      {toast && <Toast message={toast} />}
    </article>
  );
}
