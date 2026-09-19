import { contact, mailtoUrl } from "../lib/contact";
import type { Project } from "../types";
import DemoSwitcher from "./DemoSwitcher";

const STATUS_TEXT: Record<Project["status"], string> = {
  shipped: "已上线",
  "in-progress": "开发中",
};

// 作品卡：名称/状态 → 一句话卖点 → 综述 → 要点 → 技术栈 → 进入系统 → 三端演示。
//
// 「进入系统」的三态是本卡最要紧的部分（也是将来接演示环境的唯一改动点）：
//   · `links.live` 有值 → 真链接，新窗口打开；
//   · 没有值            → **禁用态按钮 + 邮件联系**，并明确写出「为什么还不能进」。
// 这就是为什么演示环境还没落地也能先把页面放出去：访客不会点进一个 404。
export default function ProjectCard({ project }: { project: Project }) {
  return (
    <article className="card">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-xl font-semibold tracking-tight">{project.name}</h3>
        <span className="chip text-brand-600 dark:text-brand-500">{STATUS_TEXT[project.status]}</span>
        <span className="text-sm text-ink-500 dark:text-ink-100/60">{project.period}</span>
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
        {project.links.live ? (
          <a className="btn-primary" href={project.links.live} target="_blank" rel="noreferrer noopener">
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

      {!project.links.live && (
        <p className="mt-3 text-xs text-ink-500 dark:text-ink-100/60">
          演示环境（域名 + 只读演示账号 + 成本护栏）尚在部署中；接入后此处会变成可直接操作的入口。
        </p>
      )}

      <DemoSwitcher project={project} />
    </article>
  );
}
