// Metro 配置（RN 侧唯一需要“跨目录取共享契约层”的地方，也是本仓最容易流血的一处）。
//
// 两条硬约束（与 mobile-h5/vite.config.ts 的注释同因同解）：
//   1) watchFolders：共享层在 ../frontend/src（本项目根目录之外）—— 不放进 watchFolders，
//      Metro 既不会监听它，也可能解析失败：
//   2) **单例锁定**：共享层内部 `import react / react-native / axios / zustand / @tanstack/react-query`
//      会按它自己的目录向上找 → 命中 frontend/node_modules（另一份 React），
//      症状是 hooks 直接崩（"Invalid hook call" / `Cannot read property 'useCallback' of null`），
//      单测与真机同错。H5 用 vite 的 alias + dedupe 解决，这里用 extraNodeModules（Metro 会**优先**查它）。
const path = require("path");

const { getDefaultConfig } = require("expo/metro-config");

const projectRoot = __dirname;
const coreRoot = path.resolve(projectRoot, "../frontend/src");

/** 必须“全仓只有一份”的包（版本与 frontend、mobile-h5 完全一致）。 */
const SINGLETONS = ["react", "react-native", "axios", "zustand", "@tanstack/react-query"];

const config = getDefaultConfig(projectRoot);

// 1) 共享契约层在项目根之外 → 必须显式放行
config.watchFolders = [...(config.watchFolders ?? []), coreRoot];

// 2) 单例锁定：bare specifier 优先解析到本项目的 node_modules
config.resolver.extraNodeModules = {
  ...(config.resolver.extraNodeModules ?? {}),
  ...Object.fromEntries(
    SINGLETONS.map((name) => [name, path.resolve(projectRoot, "node_modules", name)]),
  ),
};
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, "node_modules"),
  ...(config.resolver.nodeModulesPaths ?? []),
];

module.exports = config;
