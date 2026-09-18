// SSE 实时展示（移动版）：替代桌面 `StreamingDisplay`。
//
// **语义逐条照搬**（它承载的是 PA 与 PP 的三个关键差异，照抄 PP 会错 —— 见 @pa/core/services/sse 顶部注释）:
//   1) `hitl.waiting` 是**终态**：收到即停重连、显示「已转人工审批」，**不要**等 `approval.resumed`；
//   2) `ready` 是服务端控制帧（它认为没有进行中任务）：停止 + 通知父级刷新纠偏，不空转重连；
//   3) 注释帧 `: stream-idle-close` / `: stream-error` 不触发 onmessage → 由连接器回调转成状态文案。
// 移动端差异（手机场景）: 正文区固定高度 + 自动滚底（用户往上翻时不打断）；配图点开可放大。
import { Button, Image, ImageViewer, Tag } from "antd-mobile";
import { useEffect, useRef, useState } from "react";

import { connectProductStream, type SseFrame } from "@pa/core/services/sse";

import { formatEvent } from "@pa/core/services/streamLabels";

/** 流式过程中已就绪的配图（可点开放大看细节）。 */
export interface LiveImage {
  url: string;
  alt?: string;
}

interface Props {
  productId: string;
  reconnectDelayMs?: number;
  /** 任意终态（done/rejected/failed/hitl.waiting）触发一次，供父级复位「进行中」状态 */
  onTerminal?: (type: string) => void;
  /** 转人工审批时通知一次（父级立刻刷新状态/按钮文案） */
  onWaiting?: () => void;
  /** 生成完成（done）时通知一次（父级拉最新内容） */
  onDone?: () => void;
  /** 服务端回 ready（它认为无进行中任务）时通知一次，供父级纠偏刷新 */
  onNoActiveTask?: () => void;
  height?: number;
}

/**
 * 实时生成面板（移动版）：打字机正文 + 阶段 + 配图。
 *
 * 挂载即连流、卸载即 abort；命中终态/`ready` 后连接器不会自动复活，
 * 重新生成必须由父级换 `key` 重挂载（同桌面端口径）。
 */
export default function StreamingPanel({
  productId,
  reconnectDelayMs,
  onTerminal,
  onWaiting,
  onDone,
  onNoActiveTask,
  height = 240,
}: Props) {
  const [text, setText] = useState("");
  const [log, setLog] = useState<SseFrame[]>([]);
  const [liveImages, setLiveImages] = useState<LiveImage[]>([]);
  const [state, setState] = useState("连接中…");
  const [fatal, setFatal] = useState<string | null>(null);
  const [attemptSeed, setAttemptSeed] = useState(0); // 「重试」按钮：+1 触发重连
  const [preview, setPreview] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true); // 用户没往上翻时自动滚到底
  const callbacks = useRef({ onTerminal, onWaiting, onDone, onNoActiveTask });
  callbacks.current = { onTerminal, onWaiting, onDone, onNoActiveTask };

  useEffect(() => {
    const ctrl = new AbortController();
    let waitingSignaled = false;

    const handleFrame = (frame: SseFrame) => {
      if (frame.comment) {
        // 注释帧：状态文案由连接器回调处理，这里只记一行备查
        setLog((prev) => [...prev.slice(-199), frame]);
        return;
      }
      if (frame.type === "content.chunk") {
        const chunk = (frame.data ?? {}) as { text?: string };
        if (chunk.text) setText((prev) => prev + chunk.text);
        return;
      }
      if (frame.type === "image.ready") {
        // AI 配图就绪：直接渲染在正文下方（图片无法「打字机」流式）
        const img = (frame.data ?? {}) as { url?: string; alt?: string };
        if (img.url) {
          setLiveImages((prev) => [...prev, { url: img.url as string, alt: img.alt }]);
          setState("✔ AI 配图已就绪，正在写入…");
        }
        return;
      }
      setLog((prev) => [...prev.slice(-199), frame]);
      if (frame.type === "hitl.waiting") {
        // PA：这是终态（服务端随即关流；等待审批可能持续数小时）。
        // 必须立刻脱离「生成中」并通知父级刷新；**不要**再等 approval.resumed。
        setState("✋ 已生成，等待人工审批中（可在「审批」页处理）");
        if (!waitingSignaled) {
          waitingSignaled = true;
          callbacks.current.onWaiting?.();
        }
        return;
      }
      if (frame.type === "done") {
        setState("生成完成");
        callbacks.current.onDone?.();
        return;
      }
      setState(`阶段：${frame.type ?? "raw"}`);
    };
    void connectProductStream({
      productId,
      signal: ctrl.signal,
      retryDelayMs: reconnectDelayMs,
      onFrame: handleFrame,
      onIdle: () => setState("空闲（服务端已关流），自动续连中…"),
      onTransientError: (reason) => setState(`连接中断：${reason}，自动重试中…`),
      onReady: () => {
        setState("（暂无进行中任务）");
        // ready = 服务端认为没有进行中任务：让父级刷新一次做**状态纠偏**
        //（典型场景：任务其实已结束，只是本页的流断在了之前 —— 否则按钮会一直停着）
        callbacks.current.onNoActiveTask?.();
      },
      onTerminal: (type) => {
        if (type === "hitl.waiting") setState("✋ 已生成，等待人工审批中");
        else if (type === "done") setState("生成完成");
        else if (type === "rejected") setState("已被驳回（商品已退回草稿）");
        else setState("生成失败（商品已退回草稿，请查看轨迹后重试）");
        callbacks.current.onTerminal?.(type);
      },
      onFatal: (reason) => {
        setState(`连接失败：${reason}`);
        setFatal(reason);
        callbacks.current.onTerminal?.("failed");
      },
    });

    return () => ctrl.abort();
    // 角色/身份变化不影响流；attemptSeed 用于「重试」按钮强制重连
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId, attemptSeed, reconnectDelayMs]);

  useEffect(() => {
    if (pinnedRef.current && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [text, log]);

  const stageLines = log
    .map((frame) => formatEvent(frame))
    .filter((line) => line.length > 0)
    .join("\n");
  const hasBody = text.length > 0 || stageLines.length > 0;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <Tag color={fatal ? "danger" : "primary"} fill="outline" style={{ flex: "0 0 auto" }}>
          {fatal ? "已断开" : "实时"}
        </Tag>
        <span style={{ flex: 1, fontSize: 13, color: "#666" }}>{state}</span>
        {fatal ? (
          <Button
            size="mini"
            color="primary"
            onClick={() => {
              setFatal(null);
              setState("重新连接中…");
              setAttemptSeed((n) => n + 1);
            }}
          >
            重新连接
          </Button>
        ) : null}
      </div>
      <div
        ref={boxRef}
        onScroll={() => {
          const el = boxRef.current;
          if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        className="pa-pre-wrap"
        style={{
          height,
          overflow: "auto",
          background: "#fafafa",
          border: "1px solid #eee",
          borderRadius: 8,
          padding: 10,
          fontSize: 13,
          lineHeight: 1.7,
        }}
      >
        {hasBody ? `${text}${text && stageLines ? "\n" : ""}${stageLines}` : "暂无事件（等待服务端推送…）"}
      </div>
      {liveImages.length > 0 ? (
        <div style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
          {liveImages.map((img, i) => (
            <Image
              key={`${img.url}-${i}`}
              src={img.url}
              alt={img.alt ?? "AI 配图"}
              fit="cover"
              width={96}
              height={96}
              onClick={() => setPreview(img.url)}
              style={{ borderRadius: 8 }}
            />
          ))}
          <ImageViewer image={preview ?? ""} visible={preview !== null} onClose={() => setPreview(null)} />
        </div>
      ) : null}
    </div>
  );
}
