#!/usr/bin/env node
// SDK 配套版本离线校验（= `npx expo install --check` 的**离线等价物**）。
//
// 为什么自己写一个:
//   `expo install --check` 需要访问 Expo 云端（api.expo.dev）拿 SDK 的配套版本表；
//   离线/受限网络下它直接 ETIMEDOUT（实测），于是"依赖是否符合 SDK"这个最关键的门禁就形同虚设。
//   而那张表其实**本地就有**：`node_modules/expo/bundledNativeModules.json`。
//
// 校验口径（与 expo 官方一致）：
//   对 package.json 里每个出现在 bundledNativeModules 中的依赖，要求已安装版本满足该范围
//   （例如 expo-file-system 必须满足 `~57.0.7`）。不满足即退出码 1。
//
// 用法: node scripts/check-sdk-versions.js
const fs = require("fs");
const path = require("path");

const projectRoot = path.resolve(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(projectRoot, "package.json"), "utf8"));
const bundled = require(path.join(projectRoot, "node_modules/expo/bundledNativeModules.json"));
const semver = require("semver");

/** 已安装版本（从 node_modules 读，而不是信 package.json 的范围 —— 两者可能不一致）。 */
function installedVersion(name) {
  try {
    const pkgPath = require.resolve(`${name}/package.json`, { paths: [projectRoot] });
    return JSON.parse(fs.readFileSync(pkgPath, "utf8")).version;
  } catch {
    return null;
  }
}

const declared = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
const problems = [];
const checked = [];

for (const [name, range] of Object.entries(bundled)) {
  if (!(name in declared)) continue; // 本项目没用到这个包 → 不参与校验
  const version = installedVersion(name);
  if (!version) {
    problems.push(`${name} 声明为 ${declared[name]}，但 node_modules 里没有安装`);
    continue;
  }
  const ok = semver.satisfies(version, range, { includePrerelease: true });
  checked.push({ name, range, version, ok });
  if (!ok) problems.push(`${name}：已装 ${version}，SDK 57 要求 ${range}`);
}

console.log(`[sdk-check] 依据 node_modules/expo/bundledNativeModules.json 校验 ${checked.length} 个包`);
for (const item of checked) {
  console.log(`  ${item.ok ? "✓" : "✗"} ${item.name.padEnd(44)} ${item.version}  (SDK: ${item.range})`);
}

if (problems.length > 0) {
  console.error("\n[sdk-check] 以下依赖与 SDK 57 不配套：");
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error("\n修复：npx expo install --fix（联网）或按 SDK 版本手工锁定后重装");
  process.exit(1);
}

console.log("\n[sdk-check] 全部与 SDK 57 配套 ✓");
