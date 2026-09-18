// Jest 配置（jest-expo 预设 + 两条必须显式声明的解析规则）。
//
// 1) `@pa/core/*` → `../frontend/src/*`：与 babel.config.js 的 module-resolver 指向同一处
//    （双保险：babel 别名在 transform 期改写，moduleNameMapper 在解析期兜底）；
// 2) **单例锁定**：共享层文件位于 ../frontend/src，jest 默认会按“文件自己的目录”向上找
//    node_modules → 命中 frontend/node_modules 的另一份 react/zustand/axios。
//    这会让共享层的 hooks 在测试里崩掉（与 Metro 的 extraNodeModules 同因同解）。
const path = require("path");

const projectRoot = __dirname;

module.exports = {
  preset: "jest-expo",
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  moduleNameMapper: {
    "^@pa/core/(.*)$": "<rootDir>/../frontend/src/$1",
    "^react$": "<rootDir>/node_modules/react",
    "^react-dom$": "<rootDir>/node_modules/react-dom",
    "^react-native$": "<rootDir>/node_modules/react-native",
    "^axios$": "<rootDir>/node_modules/axios",
    "^zustand$": "<rootDir>/node_modules/zustand",
    "^@tanstack/react-query$": "<rootDir>/node_modules/@tanstack/react-query",
  },
  // Expo/RN 生态大量包以 ESM/Flow 源码发布，必须交给 babel 转译（照 jest-expo 官方口径）
  transformIgnorePatterns: [
    "node_modules/(?!(?:.pnpm/)?((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@sentry/react-native|native-base|react-native-svg|@tanstack/.*|react-native-paper))",
  ],
  testMatch: ["<rootDir>/src/**/__tests__/**/*.test.{ts,tsx}"],
  // 覆盖率只统计本模块的薄逻辑层（platform + services）。
  //
  // ⚠️ 两个「移动端共用文件」（`../frontend/src/services/{mobileFormat,streamLabels}.ts`）**不在这里统计**：
  //    它们的测试在 **mobile-h5** 套件（`format.test.ts` / `mobileServices.test.ts`），门禁也挂在那边的
  //    `allowExternal` 上。实测试过把它们收到这里：由于本套件没有它们的用例，覆盖率直接掉到 67% < 80%
  //    —— "测试在那头、门禁在这头"必然不达标，属于错配而不是"守得更严"。
  collectCoverageFrom: [
    "<rootDir>/src/platform/**/*.{ts,tsx}",
    "<rootDir>/src/services/**/*.{ts,tsx}",
    "!<rootDir>/src/**/__tests__/**",
  ],
  coverageThreshold: {
    global: { statements: 80, branches: 75, functions: 75, lines: 80 },
  },
  coverageDirectory: path.join(projectRoot, "coverage"),
};
