import type { Lang } from "../types";

// 界面语言的**唯一开关**（与 `lib/asset.ts` 的 `DEMO_BASE_URL`、`lib/entry.ts` 的 env 同款
// 「一处定义」）：语言集合、默认值、持久化 key、`<html lang>` 映射都只在这里写一次，
// 组件不许自己拼字符串，也不许各处各写一份 `["zh","en"]`。
//
// 为什么**不引 i18n 库**（i18next / react-intl）：本模块的卖点是零后端依赖的纯静态页，
// 而这里真正需要的只有「两个语言 + 一份字典 + 一次持久化」。库里那些复数 / 命名空间 /
// 日期格式化都用不上，换来的却是几十 KB 与一套要维护的配置；带参文案写成函数
// （见 `data/strings.ts`）比模板插值更直接，还能被 tsc 检查。
//
// 副作用纪律：本文件**全是纯函数**，localStorage 由调用方注入（`readStoredLang(storage)`）——
// 与 `entryUrl(platform, links, env)` 同款，单测不必 stub 全局对象、也不必重新导入模块
// （语言值本身就是一次函数调用的参数，不存在「模块加载时求值」的时序问题）。

/** 允许的语言（顺序 = 右上角开关里按钮的顺序：先中文、后英文）。 */
export const LANGS: readonly Lang[] = ["zh", "en"];

/**
 * 默认语言。**故意是中文**：作品内容的中文版是事实源（`data/projects.ts`），
 * 且默认值一改，现有访客与既有用例的语义都会跟着变 —— 默认中文是「不变的那一侧」。
 */
export const DEFAULT_LANG: Lang = "zh";

/** 语言 → `<html lang>` 的值（切换时写到 `documentElement` 上：屏幕阅读器与搜索引擎都看它）。 */
export const HTML_LANG: Record<Lang, string> = { zh: "zh-CN", en: "en" };

/**
 * localStorage 的键。带 `pa-portfolio-` 前缀：业务前端挂在同域根路径，
 * 通用名（如 `lang`）迟早和其它应用串台。
 */
export const STORAGE_KEY = "pa-portfolio-lang";

/** 类型守卫：localStorage 里可能是任何东西（旧版本写入、用户手改、别的站同名 key），一律当没有。 */
export function isLang(value: unknown): value is Lang {
  return value === "zh" || value === "en";
}

/**
 * 读访客上次选的语言；没有 / 存的是垃圾值 / 读不动 → `undefined`（调用方回退默认语言）。
 *
 * 为什么必须 try/catch：Safari 隐私模式下**访问 localStorage 就会抛**（不是返回 null），
 * 而宣传页很可能先被丢到这种环境里给人看 —— 不能因为读不到偏好就白屏。
 */
export function readStoredLang(storage?: Storage | null): Lang | undefined {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    return isLang(raw) ? raw : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 写访客选的语言；写成功返回 true。
 *
 * 失败**不抛也不提示**：写不进去只影响「下次打开还是默认语言」，
 * 而当前这次切换必须照常生效（语言是页面状态，不是持久化状态的从属）。
 */
export function writeStoredLang(storage: Storage | null | undefined, lang: Lang): boolean {
  if (!storage) return false;
  try {
    storage.setItem(STORAGE_KEY, lang);
    return true;
  } catch {
    return false;
  }
}

/**
 * 浏览器 localStorage 的取用点（唯一）。取不到（SSR / 隐私模式 / 用户禁用存储）返回 null，
 * 于是「读偏好」「写偏好」一起优雅退化成默认语言，页面其余部分照常工作。
 */
export function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}
