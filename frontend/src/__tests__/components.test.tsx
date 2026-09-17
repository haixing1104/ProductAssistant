// 路由/组件级用例：图文渲染器（含老数据退化）+ 心跳判据 + 商品详情「审批与驳回复盘」的数据期望。
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import BlockRenderer, { blocksToPlainText } from "../components/BlockRenderer";
import { heartbeatMeta } from "../pages/OpsPage";
import type { ContentBlock, WorkerHeartbeat } from "../types/api";

describe("BlockRenderer", () => {
  it("渲染 text 与 image 混排（两个消费方共用同一渲染器）", () => {
    const blocks: ContentBlock[] = [
      { type: "text", text: "第一段文案" },
      { type: "image", url: "https://example.test/a.png", alt: "配图" },
    ];
    render(<BlockRenderer blocks={blocks} />);
    expect(screen.getByText("第一段文案")).toBeInTheDocument();
    expect(screen.getByAltText("配图")).toHaveAttribute("src", "https://example.test/a.png");
  });

  it("空 blocks / null 退化为「（无内容）」而不抛错", () => {
    render(<BlockRenderer blocks={null} />);
    expect(screen.getByText("（无内容）")).toBeInTheDocument();
  });

  it("url 为空的 image block 不渲染 img（老数据兼容）", () => {
    render(<BlockRenderer blocks={[{ type: "image" }, { type: "text", text: "只有文字" }]} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("只有文字")).toBeInTheDocument();
  });

  it("blocksToPlainText 只拼接 text block", () => {
    expect(blocksToPlainText([{ type: "text", text: "a" }, { type: "image", url: "u" }, { type: "text", text: "b" }])).toBe(
      "a\nb",
    );
    expect(blocksToPlainText(undefined)).toBe("");
  });
});

describe("heartbeatMeta（运维面板的核心判据）", () => {
  it("alive=false → 红色「已停止」（键已过期 = 进程没了）", () => {
    const worker: WorkerHeartbeat = { consumer: "w1", ttl_seconds: -2, alive: false };
    expect(heartbeatMeta(worker)).toEqual({ label: "已停止（键已过期）", color: "red" });
  });

  it("TTL 很小 → 橙色预警（避免「看着活着其实刚断」）", () => {
    expect(heartbeatMeta({ consumer: "w1", ttl_seconds: 5, alive: true }).color).toBe("orange");
  });

  it("TTL 充足 → 绿色存活", () => {
    expect(heartbeatMeta({ consumer: "w1", ttl_seconds: 30, alive: true })).toEqual({
      label: "存活（TTL 30s）",
      color: "green",
    });
  });

  it("扫描失败（error 字段）不误报为死亡", () => {
    expect(heartbeatMeta({ error: "scan 失败：TimeoutError" })).toEqual({
      label: "扫描失败",
      color: "default",
    });
  });
});
