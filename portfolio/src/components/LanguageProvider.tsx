import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { STRINGS } from "../data/strings";
import type { Strings } from "../data/strings";
import { browserStorage, DEFAULT_LANG, HTML_LANG, readStoredLang, writeStoredLang } from "../lib/i18n";
import type { Lang } from "../types";

// 界面语言的**状态持有者**（全站唯一）：`lang` + `setLang` + 当前语言的字典 `t`。
//
// 为什么用 context 而不是继续往子组件传参：语言几乎每个组件都要读（连 `alt`、`aria-label`
// 都要跟着变），而 `ProjectCard` → `DemoSwitcher` → `DemoPlayer` 是三层；
// 「提升到最近的公共父级」是本仓对**某个局部状态**（如当前端）的取舍，不适用于全站文案。
// 用的是 React 自带的 context —— 零依赖，与本模块「不引库」的纪律一致。
//
// 初值**同步**从 localStorage 读（不是 `useEffect` 里补一帧）：访客上次选过英文时，
// 首帧就是英文，不会先闪一下中文再跳。三个副作用（持久化 / `<html lang>` / 标题）收在一处，全部跟着 `lang` 走。
type LangContextValue = {
  lang: Lang;
  setLang: (lang: Lang) => void;
  /** 当前语言的 UI 文案字典（`STRINGS[lang]`）。组件只读它，不要直接 import `STRINGS`。 */
  t: Strings;
};

/**
 * 默认值 = 中文 + 空实现：组件在**没有 Provider 时**（单测里直接 `render(<ProjectCard/>)`）
 * 按中文渲染，于是组件级用例不必人人包一层 Provider；而语言切换必须走真实 Provider，
 * 所以在默认上下文里点开关是**什么都不发生**的（不会静默把语言改掉却不生效）。
 */
const LangContext = createContext<LangContextValue>({
  lang: DEFAULT_LANG,
  setLang: () => {},
  t: STRINGS[DEFAULT_LANG],
});

/** 取当前语言与字典。返回的 `t` 就是各组件里所有文案的来源。 */
export function useLang(): LangContextValue {
  return useContext(LangContext);
}

type LanguageProviderProps = {
  children: ReactNode;
  /** 只给测试用：跳过 localStorage 直接指定初始语言（页面入口不传，走访客上次的选择）。 */
  initialLang?: Lang;
};

export function LanguageProvider({ children, initialLang }: LanguageProviderProps) {
  const [lang, setLangState] = useState<Lang>(
    () => initialLang ?? readStoredLang(browserStorage()) ?? DEFAULT_LANG,
  );

  useEffect(() => {
    // 三件事都必须跟着语言走：
    //   · 落盘 → 下次打开还是这个语言（写失败只影响下次，不影响本次，见 writeStoredLang）；
    //   · `<html lang>` → 屏幕阅读器选语音、搜索引擎判语种（`index.html` 里那份是构建期默认值）；
    //   · 标签页标题 → 静态 HTML 的 `<title>` 是中文，切到英文时浏览器标签也得跟着换。
    writeStoredLang(browserStorage(), lang);
    document.documentElement.lang = HTML_LANG[lang];
    document.title = STRINGS[lang].meta.title;
  }, [lang]);

  const setLang = useCallback((next: Lang) => setLangState(next), []);
  const value = useMemo<LangContextValue>(() => ({ lang, setLang, t: STRINGS[lang] }), [lang, setLang]);

  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}
