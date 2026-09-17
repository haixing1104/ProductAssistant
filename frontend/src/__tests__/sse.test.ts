// SSE 解析与走向判断的用例 —— 本文件是 P7 最重要的一组断言。
//
// 为什么重要：三类 PA 专属语义（`hitl.waiting` 是终态 / `ready` 控制帧 / 注释帧区分空闲与异常）
// 一旦写错，表现是「页面永远在转圈重连」或「空闲被当成故障报红」—— 而这些在浏览器里很难复现调试。
import { describe, expect, it } from "vitest";

import { TERMINAL_EVENT_TYPES, frameOutcome, parseSseFrame, streamUrl } from "../services/sse";

describe("parseSseFrame", () => {
  it("解析事件帧（id + data 的 JSON）", () => {
    const frame = parseSseFrame(['id: 1700000000000-0', 'data: {"type":"content.chunk","data":{"text":"你好"}}']);
    expect(frame).toEqual({ id: "1700000000000-0", type: "content.chunk", data: { text: "你好" } });
  });

  it("解析注释帧（`: stream-idle-close (last_id=…)`）—— 不解析就分不清空闲与异常", () => {
    const frame = parseSseFrame([": stream-idle-close (last_id=1700000000000-3)"]);
    expect(frame?.comment).toContain("stream-idle-close");
    expect(frame?.type).toBeUndefined();
  });

  it("解析 `: stream-error` 注释帧", () => {
    const frame = parseSseFrame([": stream-error"]);
    expect(frame?.comment).toBe("stream-error");
  });

  it("容忍 CRLF 与空行", () => {
    const frame = parseSseFrame(["", 'data: {"type":"done","data":{}}\r', ""]);
    expect(frame?.type).toBe("done");
  });

  it("data 非 JSON 时降级为 raw（不抛错）", () => {
    const frame = parseSseFrame(["data: 这不是 JSON"]);
    expect(frame).toEqual({ id: undefined, type: "raw", data: "这不是 JSON" });
  });

  it("无 data 无注释 → null（心跳帧）", () => {
    expect(parseSseFrame(["", ""])).toBeNull();
  });
});

describe("frameOutcome", () => {
  it("终态集合含 hitl.waiting（PA 把它当终态，PP 不是）", () => {
    expect(TERMINAL_EVENT_TYPES.has("hitl.waiting")).toBe(true);
    expect([...TERMINAL_EVENT_TYPES].sort()).toEqual(["done", "failed", "hitl.waiting", "rejected"]);
    expect(frameOutcome({ type: "hitl.waiting" })).toBe("terminal");
    expect(frameOutcome({ type: "done" })).toBe("terminal");
    expect(frameOutcome({ type: "rejected" })).toBe("terminal");
    expect(frameOutcome({ type: "failed" })).toBe("terminal");
  });

  it("ready 控制帧 → ready（无进行中任务，不重连）", () => {
    expect(frameOutcome({ type: "ready" })).toBe("ready");
  });

  it("普通过程事件 → null（继续读）", () => {
    for (const type of [
      "generate.started",
      "stage.researching",
      "agent.tool",
      "content.chunk",
      "image.ready",
      "approval.resumed",
    ]) {
      expect(frameOutcome({ type })).toBeNull();
    }
  });

  it("注释帧区分空闲关流与读流异常", () => {
    expect(frameOutcome({ comment: "stream-idle-close (last_id=1-0)" })).toBe("idle");
    expect(frameOutcome({ comment: "stream-error" })).toBe("error");
    expect(frameOutcome({ comment: "ping" })).toBeNull(); // 其它注释帧忽略
  });
});

describe("streamUrl", () => {
  it("指向 backend 的 SSE 端点（相对路径，走代理/同源反代）", () => {
    expect(streamUrl("abc")).toBe("/api/v1/products/abc/stream");
  });
});
