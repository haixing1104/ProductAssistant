import type { Platform, ProjectLinks } from "../types";

/**
 * 「开始使用」入口（作品卡标题行右侧）的基址解析。
 *
 * 三层口径（与 `lib/asset.ts` 的 `DEMO_BASE_URL` 同款「唯一开关」）：
 *   1. **env 注入**（优先）—— `portfolio/.env.development` 的 `VITE_ENTRY_*` 指向本机端口，
 *      dev 下点进去就是真登录页（:5173 = frontend、:5174 = mobile-h5）；
 *   2. **数据提供**（生产）—— `data/projects.ts` 的 `links.live`（PC Web）/ `links.liveH5`（H5，
 *      不填回退 `live`）。那条门禁要求 https，所以 dev 的 `http://localhost` **进不了数据**，
 *      两个环境天然分层；
 *   3. **都没有 → `undefined`**：调用方不渲染入口。页面上绝不出现点了 404 的按钮，
 *      底部的禁用占位态（「演示环境准备中」+ 邮件联系）继续兜底。
 *
 * 原生端（`rn`）**恒为 `undefined`**：RN 是装在手机里的 App，浏览器里没有可跳的 URL。
 * 点击时页面上给 Toast（`APP_DOWNLOAD_NOTE`），而不是伪造一个下载链接。
 */
export const LOGIN_PATH = "/login";

/** 原生端（RN）在浏览器里没有入口时的提示文案（Toast 内容）。 */
export const APP_DOWNLOAD_NOTE =
  "App 下载暂未开放（Android / iOS 安装包尚未上架）；可先切到 Mobile H5 在浏览器里体验。";

/** 两个端各自的基址（PC Web / Mobile H5）。 */
export type EntryBases = { web: string; h5: string };

/**
 * 环境变量读取点（唯一）。默认参数**在调用时求值**（不是模块加载时），
 * 所以单测可以直接传参做确定性用例，不必 stub env 再重新导入模块。
 */
export function envEntryBases(): EntryBases {
  return {
    web: import.meta.env.VITE_ENTRY_BASE_URL ?? "",
    h5: import.meta.env.VITE_ENTRY_H5_BASE_URL ?? "",
  };
}

/** 基址 + 登录路径。容忍基址带/不带尾斜杠，也容忍基址本身含子路径（`https://host/app`）。 */
export function joinLogin(base: string): string {
  return `${base.replace(/\/+$/, "")}${LOGIN_PATH}`;
}

/** 某个端的入口基址；`undefined` = 这个端没有入口（原生端恒无，或两处都没配置）。 */
export function entryBase(
  platform: Platform,
  links: ProjectLinks,
  env: EntryBases = envEntryBases(),
): string | undefined {
  // 原生端不是"配没配"的问题，而是根本不存在可跳的 URL
  if (platform === "rn") return undefined;

  // env 非空即整体覆盖数据（"唯一开关"口径）：dev 想临时指向别人的机器只改 .env.development
  const fromEnv = platform === "h5" ? env.h5 || env.web : env.web;
  // H5 没单独配就回退 PC Web：同一套系统的两个入口，只配一个也能用
  const fromData = platform === "h5" ? links.liveH5 || links.live : links.live;
  return fromEnv || fromData || undefined;
}

/** 某个端的入口 URL（= 基址 + `/login`）；`undefined` = 不渲染入口。 */
export function entryUrl(platform: Platform, links: ProjectLinks, env?: EntryBases): string | undefined {
  const base = entryBase(platform, links, env ?? envEntryBases());
  return base ? joinLogin(base) : undefined;
}
