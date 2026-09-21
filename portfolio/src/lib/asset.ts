import type { DemoClip, Platform } from "../types";

/** 允许的三个端（测试用它校验数据里没有拼错的平台名；页面 Tab 顺序 = demos 数组顺序）。 */
export const PLATFORMS: readonly Platform[] = ["web", "h5", "rn"];

/** 演示资产的扩展名。webm/mp4 是视频，webp 是首帧图，gif 是兜底动图。 */
export type AssetExt = "mp4" | "webm" | "webp" | "gif";

/**
 * 演示资产的基址。**空字符串 = 用本仓 `public/` 里的文件**（默认，静态零后端）。
 *
 * 将来要搬到对象存储 / CDN：只改这一行（例如 `"https://cdn.example.com/pa/"`），
 * 页面与测试都不用动 —— 这就是「本地 / 远端双模」的唯一开关。
 */
export const DEMO_BASE_URL = "";

/**
 * 生成演示资产的**规范路径**（相对 `public/`）。
 *
 * 约定：`demos/<slug>/<platform>/<key>.<ext>` —— 一个端可以放多段（`<key>` 区分），
 * 例如 `demos/pa/web/01-list.gif`、`demos/pa/rn/01-generate.gif`。
 * 放好文件后在 `data/projects.ts` 里写 `gif: demoPath("pa", "web", "01-list", "gif")` 即可；
 * 命名不一致的资产不被支持（路径可预测，才能让「声明即校验」的测试生效）。
 */
export function demoPath(slug: string, platform: Platform, key: string, ext: AssetExt): string {
  return `demos/${slug}/${platform}/${key}.${ext}`;
}

/**
 * 把 `public/` 下的相对路径拼成可直接用的 URL。
 *
 * `base` 默认取 `DEMO_BASE_URL`（非空则指向对象存储），为空时跟随 Vite 的 `BASE_URL`
 * —— 这样部署到子路径（`https://host/portfolio/`）时不用改任何代码。
 */
export function publicFile(relativePath: string, base: string = DEMO_BASE_URL): string {
  const prefix = base || import.meta.env.BASE_URL || "/";
  return `${prefix.endsWith("/") ? prefix : `${prefix}/`}${relativePath.replace(/^\/+/, "")}`;
}

/** `DemoAsset` / `DemoClip` 共有的可渲染字段（优先级只在 `resolveDemoSource()` 里定义一次）。 */
export type DemoResource = Pick<DemoClip, "video" | "poster" | "gif" | "note">;

/** `DemoResource` 降级后的三种形态，组件只认这三种，不再各自判断字段。 */
export type DemoSource =
  | { kind: "video"; src: string; poster?: string }
  | { kind: "gif"; src: string }
  | { kind: "placeholder"; note: string };

/** 没录资产时的默认文案（数据里没写 `note` 也不至于渲染出空白）。 */
export const PLACEHOLDER_NOTE = "演示录制中";

/**
 * 决定一段（或一整个端）用哪种形态渲染：**video 优先、其次 gif、最后占位**。
 *
 * 这个优先级只写一次（组件与测试共用），避免「组件里 if 的顺序」与「测试的期望」各说一套。
 */
export function resolveDemoSource(resource: DemoResource): DemoSource {
  if (resource.video) {
    return resource.poster
      ? { kind: "video", src: resource.video, poster: resource.poster }
      : { kind: "video", src: resource.video };
  }
  if (resource.gif) {
    return { kind: "gif", src: resource.gif };
  }
  return { kind: "placeholder", note: resource.note?.trim() || PLACEHOLDER_NOTE };
}

/**
 * 选出当前生效的那一段：按 `key` 找，找不到（或没选）就回退到第一段。
 *
 * 回退而不是渲染空白：`key` 只在切换段时由组件写入，回退保证任何情况下舞台都有内容，
 * 也顺带覆盖了「某一端只有一段」（此时页面根本不渲染分段卡片）。
 */
export function resolveClip(clips: DemoClip[], key: string | undefined): DemoClip | undefined {
  if (clips.length === 0) return undefined;
  return clips.find((clip) => clip.key === key) ?? clips[0];
}

/**
 * 解析 `aspect` 这种 CSS 值（如 `"1882 / 912"`）成数值比例。
 * 用在「竖屏端要收窄居中」这个判断上 —— 由数据里的真实比例决定，而不是硬编码端名。
 */
export function aspectRatio(aspect: string): number {
  const [width, height] = aspect.split("/").map((part) => Number.parseFloat(part.trim()));
  if (!width || !height || Number.isNaN(width) || Number.isNaN(height)) return 1;
  return width / height;
}

/** 竖屏素材（手机截图）：舞台需要收窄并居中，否则 400px 宽的截图会被拉成整页宽。 */
export function isPortrait(aspect: string): boolean {
  return aspectRatio(aspect) < 1;
}

