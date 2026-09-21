import { HTML_LANG, LANGS } from "../lib/i18n";
import { useLang } from "./LanguageProvider";

// 首屏右上角的语言开关：两段式 `[中][EN]`，**当前语言高亮**。
//
// 为什么是两段而不是「单个按钮显示另一种语言」：
//   · 用户要的就是「中 / EN」这个可见的对照；单按钮下 `EN` 到底是"当前语言"还是"点了会变"要靠猜；
//   · 两段 + `aria-pressed` 让屏幕阅读器能读出**哪一项被选中**（单个 toggle 只能读"未按下"）。
// 按钮上的字永远写「中」与「EN」本身（两种界面语言下都一样）—— 语言名不翻译，这是通用做法，
// 也是 `__tests__/strings.test.ts` 里「英文包不得残留中文」门禁唯一豁免的那一处。
//
// 每个按钮各带自己的 `lang` 属性：中文按钮上是 `lang="zh-CN"`，英文按钮上是 `lang="en"` ——
// 屏幕阅读器会按该语言的发音规则念，不会用中文语音去念「EN」。
export default function LangSwitch({ className }: { className?: string }) {
  const { lang, setLang, t } = useLang();

  return (
    <div
      role="group"
      aria-label={t.langSwitch.group}
      className={[
        "inline-flex shrink-0 items-center gap-0.5 rounded-full border border-ink-200 p-0.5",
        "dark:border-white/15",
        className ?? "",
      ].join(" ")}
    >
      {LANGS.map((item) => {
        const selected = item === lang;
        return (
          <button
            key={item}
            type="button"
            lang={HTML_LANG[item]}
            aria-pressed={selected}
            onClick={() => setLang(item)}
            className={[
              "rounded-full px-2.5 py-1 text-xs font-medium transition outline-none",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600",
              selected
                ? "bg-brand-600 text-white"
                : "text-ink-500 hover:text-ink-900 dark:text-ink-100/60 dark:hover:text-ink-50",
            ].join(" ")}
          >
            {item === "zh" ? t.langSwitch.zh : t.langSwitch.en}
          </button>
        );
      })}
    </div>
  );
}
