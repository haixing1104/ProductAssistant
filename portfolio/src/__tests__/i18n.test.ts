import {
  browserStorage,
  DEFAULT_LANG,
  HTML_LANG,
  isLang,
  LANGS,
  readStoredLang,
  STORAGE_KEY,
  writeStoredLang,
} from "../lib/i18n";

// 语言开关的薄逻辑层：语言集合 / 默认值 / `<html lang>` 映射 / localStorage 读写的**全部边界**。
//
// 读写都走**注入的 storage**（不是全局 localStorage）—— 与 `entryUrl(platform, links, env)` 同款口径，
// 于是「存的是垃圾值」「读的时候抛异常」这类分支都能直接构造，不用 stub 全局对象、
// 也不用「改 env 再重新导入模块」那种脆弱写法。

/** 最小 Storage 实现：既是夹具，也是「IO 用参数注入」这个口径的样板。 */
function fakeStorage(initial: Record<string, string> = {}): Storage {
  const map = new Map(Object.entries(initial));
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => {
      map.delete(key);
    },
    setItem: (key: string, value: string) => {
      map.set(key, String(value));
    },
  };
}

describe("语言常量", () => {
  it("两种语言、默认中文、`<html lang>` 映射齐全", () => {
    expect(LANGS).toEqual(["zh", "en"]);
    expect(DEFAULT_LANG).toBe("zh");
    expect(HTML_LANG).toEqual({ zh: "zh-CN", en: "en" });
    // 键必须与 LANGS 完全一致：将来加了第三种语言却忘了补映射，这里就红
    expect(Object.keys(HTML_LANG).sort()).toEqual([...LANGS].sort());
  });

  it("localStorage 的键带模块前缀（与同域的业务前端不串台）", () => {
    expect(STORAGE_KEY).toBe("pa-portfolio-lang");
  });
});

describe("isLang：类型守卫", () => {
  it("只认 zh / en（大小写、BCP-47 全称、空串、别的语言、非字符串一律不算）", () => {
    expect(isLang("zh")).toBe(true);
    expect(isLang("en")).toBe(true);

    for (const value of ["ZH", "En", "", "fr", "zh-CN", null, undefined, 0, 42, {}, []]) {
      expect(isLang(value), `${String(value)} 不该被当成语言`).toBe(false);
    }
  });
});

describe("readStoredLang：读访客上次选的语言", () => {
  it("存过就返回它", () => {
    expect(readStoredLang(fakeStorage({ [STORAGE_KEY]: "en" }))).toBe("en");
  });

  it("没存过 / 存的是垃圾值 / 只有同名但不同 key → undefined（调用方回退默认语言）", () => {
    expect(readStoredLang(fakeStorage())).toBeUndefined();
    expect(readStoredLang(fakeStorage({ [STORAGE_KEY]: "ja" }))).toBeUndefined();
    expect(readStoredLang(fakeStorage({ "other-key": "en" }))).toBeUndefined();
  });

  it("没有 storage（SSR / 用户禁用存储）或读的时候抛异常（Safari 隐私模式）→ undefined，不抛", () => {
    expect(readStoredLang(undefined)).toBeUndefined();
    expect(readStoredLang(null)).toBeUndefined();

    const throwing = { ...fakeStorage(), getItem: () => {
      throw new Error("SecurityError");
    } };
    expect(readStoredLang(throwing as Storage)).toBeUndefined();
  });
});

describe("writeStoredLang：写访客选的语言", () => {
  it("写得进去 → true，且下次读得出来（含覆盖上一次的选择）", () => {
    const storage = fakeStorage();

    expect(writeStoredLang(storage, "en")).toBe(true);
    expect(readStoredLang(storage)).toBe("en");

    expect(writeStoredLang(storage, "zh")).toBe(true);
    expect(readStoredLang(storage)).toBe("zh");
  });

  it("没有 storage → false；写的时候抛异常（配额满 / 隐私模式）→ false 且**不抛**（本次切换仍生效）", () => {
    expect(writeStoredLang(null, "en")).toBe(false);
    expect(writeStoredLang(undefined, "en")).toBe(false);

    const throwing = { ...fakeStorage(), setItem: () => {
      throw new Error("QuotaExceededError");
    } };
    expect(writeStoredLang(throwing as Storage, "en")).toBe(false);
  });
});

describe("browserStorage：浏览器存储的取用点", () => {
  it("jsdom 下就是 window.localStorage（取不到才算 null）", () => {
    expect(browserStorage()).toBe(window.localStorage);
  });
});
