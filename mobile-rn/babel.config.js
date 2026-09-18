// Babel 配置（只有一个自定义点：把共享契约层的别名 `@pa/core` 指向 `../frontend/src`）。
//
// 为什么用 babel 别名而不是 tsconfig paths + Expo 的 tsconfigPaths 实验开关：
//   显式、可读、失败时报错位置明确，而且 **jest 与 Metro 走同一份 babel 配置**（jest-expo 用 babel-jest），
//   一处配置两端生效 —— 与 mobile-h5 用 vite alias + vitest 复用同一份解析的取舍一致。
//
// 为什么必须给绝对路径：相对路径由 plugin 按“配置文件所在目录”解析，写绝对路径可以避免
// 被 jest 的 cwd（<rootDir>）与 Metro 的 projectRoot 差异搞出两套解析结果。
const path = require("path");

module.exports = function (api) {
  api.cache(true);
  return {
    presets: ["babel-preset-expo"],
    plugins: [
      [
        "module-resolver",
        {
          alias: {
            // 用法：@pa/core/api、@pa/core/services/http、@pa/core/types/api…
            "@pa/core": path.resolve(__dirname, "../frontend/src"),
          },
          // ⚠️ 必须显式给 extensions：插件默认列表**不含 `.ts`**，
          // 于是 `@pa/core/api`（目录 import，实际是 `api/index.ts`）解析失败 →
          // 插件告警 "Could not resolve …" 并**保持原样**，Metro 随后就会因为找不到 `@pa/core` 而打包失败。
          extensions: [".ts", ".tsx", ".js", ".jsx", ".json"],
        },
      ],
    ],
  };
};
