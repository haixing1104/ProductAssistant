import type { DemoAsset, Platform } from "../types";

/** 允许的端（页面 Tab 顺序 = 这个数组顺序；测试也用它校验数据里没有拼错的平台名）。 */
export const PLATFORMS: readonly Platform[] = ["web", "h5", "rn-android", "rn-ios"];

/** 演示资产的扩展名。webm/mp4 是视频，webp 是首帧图，gif 是兜底动图。 */
export type AssetExt = "mp4" | "webm" | "webp" | "gif";

/**
 * 生成演示资产的**规范路径**（相对 `public/`）。
 *
 * 约定：`demos/<slug>/<platform>.<ext>` —— 放好文件后在 `data/projects.ts` 里写
 * `video: demoPath("pa", "web", "mp4")` 即可；命名不一致的资产不被支持（路径可预测，
 * 才能让「声明即校验」的测试生效）。
 */
export function demoPath(slug: string, platform: Platform, ext: AssetExt): string {
  return `demos/${slug}/${platform}.${ext}`;
}

/** 把 `public/` 下的相对路径拼成可直接用的 URL（跟随 `base`，兼容子路径部署）。 */
export function publicFile(relativePath: string): string {
  const base = import.meta.env.BASE_URL || "/";
  return `${base.endsWith("/") ? base : `${base}/`}${relativePath.replace(/^\/+/, "")}`;
}

/** `DemoAsset` 降级后的三种形态，组件只认这三种，不再各自判断字段。 */
export type DemoSource =
  | { kind: "video"; src: string; poster?: string }
  | { kind: "gif"; src: string }
  | { kind: "placeholder"; note: string };

/** 没录资产时的默认文案（数据里没写 `note` 也不至于渲染出空白）。 */
export const PLACEHOLDER_NOTE = "演示录制中";

/**
 * 决定一个端用哪种形态渲染：**video 优先、其次 gif、最后占位**。
 *
 * 这个优先级只写一次（组件与测试共用），避免「组件里 if 的顺序」和「README 的说明」各说一套。
 */
export function resolveDemoSource(asset: DemoAsset): DemoSource {
  if (asset.video) {
    return asset.poster
      ? { kind: "video", src: asset.video, poster: asset.poster }
      : { kind: "video", src: asset.video };
  }
  if (asset.gif) {
    return { kind: "gif", src: asset.gif };
  }
  return { kind: "placeholder", note: asset.note?.trim() || PLACEHOLDER_NOTE };
}
