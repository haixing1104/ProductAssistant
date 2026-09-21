// Hero：三件事 —— 一句定位、三条事实、一个动作（看作品），外加右上角的语言开关。
// 邮件 / GitHub 只出现在页脚联系区：同一动作不在首屏与底栏重复，首屏只留一个决策点
// （语言开关是静默的实用控件，不是行动点，也不算联系方式）。
// 刻意不放头像与装饰图：纯 CSS 背景光晕 + 文字，首屏无图片请求（静态托管的秒开靠这个）。
//
// 文案一律来自 `useLang().t`（含三条事实）：组件里不出现面向访客的硬编码字符串。

import LangSwitch from "./LangSwitch";
import { useLang } from "./LanguageProvider";

export default function Hero() {
  const { t } = useLang();

  return (
    <header className="relative overflow-hidden border-b border-ink-100 dark:border-white/10">
      {/* 背景光晕：纯 CSS（不引图片、不引动画库），深色模式下换成更暗的蓝 */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 left-1/2 h-96 w-[48rem] -translate-x-1/2 rounded-full bg-brand-100/70 blur-3xl dark:bg-brand-700/20"
      />

      <div className="relative mx-auto max-w-5xl px-6 py-20 sm:py-28">
        {/* 语言开关落在**页面右上角**：与上面那行小字同一行（`justify-between`），
            宽度不够时整行换行，也不会挤到大标题；`-mt-1` 让它与那行小字的视觉高度对齐 */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs font-medium tracking-[0.2em] text-brand-600 uppercase dark:text-brand-500">
            {t.hero.eyebrow}
          </p>
          <LangSwitch className="-mt-1" />
        </div>

        <h1 className="mt-4 text-4xl leading-tight font-semibold tracking-tight text-balance sm:text-5xl">
          {t.hero.title}
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-ink-700 dark:text-ink-100/85">
          {t.hero.intro}
        </p>

        <dl className="mt-10 flex flex-wrap gap-x-10 gap-y-4">
          {t.hero.facts.map((fact) => (
            <div key={fact.label}>
              <dt className="text-sm font-medium">{fact.label}</dt>
              <dd className="mt-0.5 text-sm text-ink-500 dark:text-ink-100/60">{fact.hint}</dd>
            </div>
          ))}
        </dl>

        <div className="mt-10 flex flex-wrap gap-3">
          <a className="btn-primary" href="#projects">
            {t.hero.cta}
          </a>
        </div>
      </div>
    </header>
  );
}
