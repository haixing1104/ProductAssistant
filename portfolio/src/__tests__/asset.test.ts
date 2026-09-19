import { demoPath, PLACEHOLDER_NOTE, PLATFORMS, publicFile, resolveDemoSource } from "../lib/asset";
import type { DemoAsset } from "../types";

describe("demoPath：规范路径", () => {
  it("按 demos/<slug>/<platform>.<ext> 生成", () => {
    expect(demoPath("pa", "web", "mp4")).toBe("demos/pa/web.mp4");
    expect(demoPath("pa", "rn-android", "webp")).toBe("demos/pa/rn-android.webp");
    expect(demoPath("pa", "h5", "gif")).toBe("demos/pa/h5.gif");
  });

  it("四个端都在允许集合里（顺序即页面 Tab 顺序）", () => {
    expect(PLATFORMS).toEqual(["web", "h5", "rn-android", "rn-ios"]);
    for (const platform of PLATFORMS) {
      expect(demoPath("pa", platform, "mp4")).toBe(`demos/pa/${platform}.mp4`);
    }
  });
});

describe("publicFile：拼 URL", () => {
  it("拼上 base，并容忍路径带/不带前导斜杠", () => {
    expect(publicFile("demos/pa/web.mp4")).toBe("/demos/pa/web.mp4");
    expect(publicFile("/demos/pa/web.mp4")).toBe("/demos/pa/web.mp4");
  });
});

describe("resolveDemoSource：video > gif > 占位", () => {
  const base: DemoAsset = { platform: "web", label: "Web 工作台" };

  it("有 video 就用 video；poster 是可选的", () => {
    expect(resolveDemoSource({ ...base, video: "demos/pa/web.mp4", poster: "demos/pa/web.webp" })).toEqual({
      kind: "video",
      src: "demos/pa/web.mp4",
      poster: "demos/pa/web.webp",
    });
    expect(resolveDemoSource({ ...base, video: "demos/pa/web.mp4" })).toEqual({
      kind: "video",
      src: "demos/pa/web.mp4",
    });
  });

  it("只有 gif 时用 gif（video 存在时 gif 只是兜底，不抢优先级）", () => {
    expect(resolveDemoSource({ ...base, gif: "demos/pa/web.gif" })).toEqual({
      kind: "gif",
      src: "demos/pa/web.gif",
    });
    expect(resolveDemoSource({ ...base, video: "demos/pa/web.mp4", gif: "demos/pa/web.gif" }).kind).toBe("video");
  });

  it("都没有时降级成占位，用 note 作文案", () => {
    expect(resolveDemoSource({ ...base, note: "录制中" })).toEqual({ kind: "placeholder", note: "录制中" });
  });

  it("note 缺失或全是空格时给默认文案 —— 页面上不留「无说明的空白」", () => {
    expect(resolveDemoSource(base)).toEqual({ kind: "placeholder", note: PLACEHOLDER_NOTE });
    expect(resolveDemoSource({ ...base, note: "   " })).toEqual({ kind: "placeholder", note: PLACEHOLDER_NOTE });
  });
});
