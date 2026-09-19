import {
  aspectRatio,
  demoPath,
  isPortrait,
  PLACEHOLDER_NOTE,
  PLATFORMS,
  publicFile,
  resolveClip,
  resolveDemoSource,
} from "../lib/asset";
import type { DemoResource } from "../lib/asset";
import type { DemoClip } from "../types";

describe("demoPath：规范路径", () => {
  it("按 demos/<slug>/<platform>/<key>.<ext> 生成（一个端多段，靠 key 区分）", () => {
    expect(demoPath("pa", "web", "01-list", "gif")).toBe("demos/pa/web/01-list.gif");
    expect(demoPath("pa", "rn", "01-overview", "webp")).toBe("demos/pa/rn/01-overview.webp");
    expect(demoPath("pa", "h5", "02-approve", "mp4")).toBe("demos/pa/h5/02-approve.mp4");
  });

  it("三个端都在允许集合里（PC Web / Mobile H5 / Mobile Native）", () => {
    expect(PLATFORMS).toEqual(["web", "h5", "rn"]);
    for (const platform of PLATFORMS) {
      expect(demoPath("pa", platform, "01-x", "gif")).toBe(`demos/pa/${platform}/01-x.gif`);
    }
  });
});

describe("publicFile：拼 URL", () => {
  it("默认拼 Vite 的 base，并容忍路径带/不带前导斜杠", () => {
    expect(publicFile("demos/pa/web/01-list.gif")).toBe("/demos/pa/web/01-list.gif");
    expect(publicFile("/demos/pa/web/01-list.gif")).toBe("/demos/pa/web/01-list.gif");
  });

  // 这一条就是「搬到对象存储只改一行」的契约：base 非空时整段换前缀（含子路径与尾斜杠两种写法）
  it("传了 CDN/OSS 基址就换成它（本地 / 远端双模）", () => {
    expect(publicFile("demos/pa/web/01-list.gif", "https://cdn.example.com/pa")).toBe(
      "https://cdn.example.com/pa/demos/pa/web/01-list.gif",
    );
    expect(publicFile("demos/pa/web/01-list.gif", "https://cdn.example.com/pa/")).toBe(
      "https://cdn.example.com/pa/demos/pa/web/01-list.gif",
    );
  });
});

describe("resolveDemoSource：video > gif > 占位", () => {
  const base: DemoResource = {};

  it("有 video 就用 video；poster 是可选的", () => {
    expect(resolveDemoSource({ ...base, video: "demos/pa/web/01-list.mp4", poster: "demos/pa/web/01-list.webp" })).toEqual(
      { kind: "video", src: "demos/pa/web/01-list.mp4", poster: "demos/pa/web/01-list.webp" },
    );
    expect(resolveDemoSource({ ...base, video: "demos/pa/web/01-list.mp4" })).toEqual({
      kind: "video",
      src: "demos/pa/web/01-list.mp4",
    });
  });

  it("只有 gif 时用 gif（video 存在时 gif 只是兜底，不抢优先级）", () => {
    expect(resolveDemoSource({ ...base, gif: "demos/pa/web/01-list.gif" })).toEqual({
      kind: "gif",
      src: "demos/pa/web/01-list.gif",
    });
    expect(resolveDemoSource({ ...base, video: "demos/pa/web/01-list.mp4", gif: "demos/pa/web/01-list.gif" }).kind).toBe(
      "video",
    );
  });

  it("都没有时降级成占位，用 note 作文案", () => {
    expect(resolveDemoSource({ ...base, note: "录制中" })).toEqual({ kind: "placeholder", note: "录制中" });
  });

  it("note 缺失或全是空格时给默认文案 —— 页面上不留「无说明的空白」", () => {
    expect(resolveDemoSource(base)).toEqual({ kind: "placeholder", note: PLACEHOLDER_NOTE });
    expect(resolveDemoSource({ ...base, note: "   " })).toEqual({ kind: "placeholder", note: PLACEHOLDER_NOTE });
  });
});

describe("resolveClip：选当前段", () => {
  const clip = (key: string): DemoClip => ({ key, title: key, duration: "10s", gif: `demos/pa/web/${key}.gif` });
  const clips = [clip("01-list"), clip("02-generate")];

  it("按 key 找到就用那一段", () => {
    expect(resolveClip(clips, "02-generate")?.key).toBe("02-generate");
  });

  it("没选（undefined）或 key 写错时回退第一段 —— 舞台不留空白", () => {
    expect(resolveClip(clips, undefined)?.key).toBe("01-list");
    expect(resolveClip(clips, "09-not-exist")?.key).toBe("01-list");
  });

  it("这一端还没录（空数组）时返回 undefined（由组件渲染占位卡）", () => {
    expect(resolveClip([], "01-list")).toBeUndefined();
  });
});

describe("aspectRatio / isPortrait：舞台比例", () => {
  it("解析 CSS 比例值（数据里写的是素材真实尺寸）", () => {
    expect(aspectRatio("1882 / 912")).toBeCloseTo(2.064, 2);
    expect(aspectRatio("493 / 854")).toBeCloseTo(0.577, 2);
  });

  it("竖屏素材要收窄居中（否则手机截图会被拉成整页宽）", () => {
    expect(isPortrait("493 / 854")).toBe(true);
    expect(isPortrait("480 / 1040")).toBe(true);
    expect(isPortrait("1882 / 912")).toBe(false);
  });

  it("值写坏时不抛异常（退回 1:1），页面不至于白屏", () => {
    expect(aspectRatio("")).toBe(1);
    expect(aspectRatio("abc")).toBe(1);
    expect(aspectRatio("0 / 0")).toBe(1);
    expect(isPortrait("abc")).toBe(false);
  });
});

