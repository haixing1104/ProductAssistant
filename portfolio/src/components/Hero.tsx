import { contact, mailtoUrl } from "../lib/contact";

// Hero：三件事 —— 一句定位、三条事实、两个动作（看作品 / 发邮件）。
// 刻意不放头像与装饰图：纯 CSS 背景光晕 + 文字，首屏无图片请求（静态托管的秒开靠这个）。

const FACTS = [
  { label: "3 端界面", hint: "桌面 / H5 / 原生 App" },
  { label: "1 套契约层", hint: "三端共用 api + store" },
  { label: "2 层服务隔离", hint: "业务网关 / AI 引擎" },
];

export default function Hero() {
  return (
    <header className="relative overflow-hidden border-b border-ink-100 dark:border-white/10">
      {/* 背景光晕：纯 CSS（不引图片、不引动画库），深色模式下换成更暗的蓝 */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 left-1/2 h-96 w-[48rem] -translate-x-1/2 rounded-full bg-brand-100/70 blur-3xl dark:bg-brand-700/20"
      />

      <div className="relative mx-auto max-w-5xl px-6 py-20 sm:py-28">
        <p className="text-xs font-medium tracking-[0.2em] text-brand-600 uppercase dark:text-brand-500">
          Portfolio · 作品集
        </p>
        <h1 className="mt-4 text-4xl leading-tight font-semibold tracking-tight text-balance sm:text-5xl">
          全栈工程 · 多端交付
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-ink-700 dark:text-ink-100/85">
          把一条业务链路从数据库做到三端界面：后端网关、AI 编排、桌面工作台、移动端 H5 与原生 App，全部自己落地。
        </p>

        <dl className="mt-10 flex flex-wrap gap-x-10 gap-y-4">
          {FACTS.map((fact) => (
            <div key={fact.label}>
              <dt className="text-sm font-medium">{fact.label}</dt>
              <dd className="mt-0.5 text-sm text-ink-500 dark:text-ink-100/60">{fact.hint}</dd>
            </div>
          ))}
        </dl>

        <div className="mt-10 flex flex-wrap gap-3">
          <a className="btn-primary" href="#projects">
            查看作品 ↓
          </a>
          <a className="btn-ghost" href={mailtoUrl(contact.email, "来自作品集的联系")}>
            邮件联系我
          </a>
        </div>
      </div>
    </header>
  );
}
