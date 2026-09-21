import { entryBase, entryUrl, envEntryBases, joinLogin, LOGIN_PATH } from "../lib/entry";
import type { EntryBases } from "../lib/entry";
import type { ProjectLinks } from "../types";

// 「开始使用」入口的地址解析：三层口径（env 覆盖 > 数据 > 都没有）＋ 原生端恒无入口。
//
// 全部走**显式传参**（`env` 是默认参数、调用时求值），所以用例不依赖 .env 文件、
// 也不用 stubEnv + 重新导入模块那套脆弱写法 —— 端口与域名都由用例自己给。

const NO_ENV: EntryBases = { web: "", h5: "" };
const DEV_ENV: EntryBases = { web: "http://localhost:5173", h5: "http://localhost:5174" };
const NO_LINKS: ProjectLinks = {};
const PROD_WEB_ONLY: ProjectLinks = { live: "https://demo.example.com" };
const PROD_BOTH: ProjectLinks = { live: "https://demo.example.com", liveH5: "https://m.example.com" };

describe("entryUrl：某个端的入口地址（基址 + /login）", () => {
  it("dev：env 注入本机端口，点了就是真登录页（PC Web / Mobile H5 各一个端口）", () => {
    expect(entryUrl("web", NO_LINKS, DEV_ENV)).toBe("http://localhost:5173/login");
    expect(entryUrl("h5", NO_LINKS, DEV_ENV)).toBe("http://localhost:5174/login");
  });

  it("prod：用数据里的 https 基址；H5 没单独配就回退 PC Web 的", () => {
    expect(entryUrl("web", PROD_BOTH, NO_ENV)).toBe("https://demo.example.com/login");
    expect(entryUrl("h5", PROD_BOTH, NO_ENV)).toBe("https://m.example.com/login");
    expect(entryUrl("h5", PROD_WEB_ONLY, NO_ENV)).toBe("https://demo.example.com/login");
  });

  it("env 非空即整体覆盖数据（唯一开关：临时指向别人的机器只改 .env.development）", () => {
    expect(entryUrl("web", PROD_BOTH, DEV_ENV)).toBe("http://localhost:5173/login");
    // H5 的 env 为空、web 的有值 → H5 回退 web 的 env（同机同源，比"没配就消失"合理）
    expect(entryUrl("h5", PROD_BOTH, { web: "http://192.168.1.9:5173", h5: "" })).toBe(
      "http://192.168.1.9:5173/login",
    );
  });

  it("基址带尾斜杠也能拼对（子路径部署时基址可能是 https://host/app/）", () => {
    expect(entryUrl("web", { live: "https://demo.example.com/app/" }, NO_ENV)).toBe(
      "https://demo.example.com/app/login",
    );
  });

  it("原生端恒无入口 —— 浏览器里没有可跳的 URL（两处都配满也一样）", () => {
    expect(entryBase("rn", PROD_BOTH, DEV_ENV)).toBeUndefined();
    expect(entryUrl("rn", PROD_BOTH, DEV_ENV)).toBeUndefined();
  });

  it("两处都没有 → undefined（调用方据此不渲染入口，而不是给一个死链）", () => {
    expect(entryUrl("web", NO_LINKS, NO_ENV)).toBeUndefined();
    expect(entryUrl("h5", NO_LINKS, NO_ENV)).toBeUndefined();
  });
});

describe("joinLogin：基址 + 登录路径", () => {
  it("容忍尾斜杠（含重复的），子路径保持原样", () => {
    expect(joinLogin("https://demo.example.com")).toBe("https://demo.example.com/login");
    expect(joinLogin("https://demo.example.com/")).toBe("https://demo.example.com/login");
    expect(joinLogin("https://demo.example.com/app")).toBe("https://demo.example.com/app/login");
    expect(joinLogin("https://demo.example.com/app//")).toBe("https://demo.example.com/app/login");
  });

  it("登录路径只出现一次（基址里再写 /login 属于配置错误，由 projects.test.ts 挡）", () => {
    expect(LOGIN_PATH).toBe("/login");
    expect(joinLogin("https://demo.example.com").split(LOGIN_PATH)).toHaveLength(2);
  });
});

describe("默认读取点", () => {
  it("envEntryBases 返回两个字符串（测试环境不读 .env.development → 空串 = 走数据或禁用态）", () => {
    const bases = envEntryBases();
    expect(typeof bases.web).toBe("string");
    expect(typeof bases.h5).toBe("string");
  });

  // 原生端「暂未开放」的 Toast 文案已随多语言迁到 `data/strings.ts`
  // （文案属于字典、本文件只留 URL 逻辑）—— 那条断言在 `__tests__/strings.test.ts`。
});
