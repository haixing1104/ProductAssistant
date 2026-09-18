// 单例锁定守卫（**照抄 mobile-h5 的事故**：共享层在项目根之外时最容易加载两份 React）。
//
// 事故现场（H5 实测）：共享层 `frontend/src/**` 内部 `import react` 会按**它自己的目录**解析到
// `frontend/node_modules` → 同一页面两份 React，症状是 hooks 直接崩：
// `Cannot read properties of null (reading 'useCallback')`（单测与真机同错）。
// 这里把 Metro 与 Jest 两条解析路径的"锁"钉死，防止有人"简化配置"时把地雷埋回去。
//
// 说明：`expo/metro-config` 在 jest 环境里跑不起来（它自身依赖 Metro 运行时），
// 所以这里把它 mock 成"空默认配置" —— 断言的是**我们自己补的那两行**（watchFolders / extraNodeModules），
// 而不是第三方的默认行为。
jest.mock("expo/metro-config", () => ({
  getDefaultConfig: () => ({
    watchFolders: [],
    resolver: { extraNodeModules: {}, nodeModulesPaths: [] },
  }),
}));

const path = require("path");

const metroConfig = require("../../metro.config.js");
const jestConfig = require("../../jest.config.js");

const projectRoot = path.resolve(__dirname, "../..");
/** 共享契约层目录（`@pa/core/*` 指向的真实位置；Metro 必须放行它才能解析）。 */
const CORE_DIR = path.resolve(projectRoot, "../frontend/src");
/** 必须**只有一份**的包：出现第二份的最典型症状是 hooks 直接崩（两份 React），Metro 与 Jest 两条路径都要锁。 */
const SINGLETONS = ["react", "react-native", "axios", "zustand", "@tanstack/react-query"];

describe("Metro 共享层解析（防两份 React）", () => {
  it("watchFolders 放行共享契约层（否则 Metro 根本看不到 ../frontend/src）", () => {
    expect(metroConfig.watchFolders).toContain(CORE_DIR);
  });

  it("所有「必须只有一份」的包都锁到本项目 node_modules", () => {
    for (const name of SINGLETONS) {
      expect(metroConfig.resolver.extraNodeModules[name]).toBe(
        path.resolve(projectRoot, "node_modules", name),
      );
    }
  });
});

describe("Jest 共享层解析（同一条约束的第二道锁）", () => {
  it("@pa/core 指向共享契约层", () => {
    expect(jestConfig.moduleNameMapper["^@pa/core/(.*)$"]).toBe("<rootDir>/../frontend/src/$1");
  });

  it("react / react-native / axios / zustand / react-query 都映射到本项目 node_modules", () => {
    for (const name of SINGLETONS) {
      expect(jestConfig.moduleNameMapper[`^${name}$`]).toBe(`<rootDir>/node_modules/${name}`);
    }
  });
});
