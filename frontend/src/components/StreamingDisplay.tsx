// SSE 流式展示（打字机正文 + 阶段流 + 图片就绪），基于 services/sse.ts 的 PA 语义连接器。
//
// 与 ProductPilot 版的三点行为差异（都是「照抄会错」的点，改动前先读 services/sse.ts 顶部注释）：
//   1) `hitl.waiting` 是**终态**：收到即显示「已转人工审批」，**不再重连**（PP 版会一直等 resume）；
//   2) `ready` 控制帧：显示「无进行中任务」并停止（不空转重连）；
//   3) 注释帧被区分处理：空闲关流 → 自动续连（带 Last-Event-ID）；读流异常 → 限量重试并告警。
import { Button, Space, Typography } from "antd";
import { useEffect, useRef, useState } from "react";

import { connectProductStream, type SseFrame } from "../services/sse";
import { formatEvent, statusLine } from "../services/streamLabels";

/** 流式过程中已就绪的配图（`image.ready` 事件累积而来，可点开大图）。 */
export interface LiveImage {
  url: string;
  alt?: string;
}

interface Props {
  productId: string;
  /** 续连/重试间隔（毫秒；默认 1500，测试注入小值去掉墙钟依赖） */
  reconnectDelayMs?: number;
  /** 任意终态（done/rejected/failed/hitl.waiting）触发一次，供父级复位「进行中」状态 */
  onTerminal?: (type: string) => void;
  /** 转人工审批（hitl.waiting）时通知一次，供父级立刻刷新商品状态/按钮文案 */
  onWaiting?: () => void;
  /** 生成完成（done）时通知一次，供父级拉取最新内容 */
  onDone?: () => void;
  /**
   * 服务端回 ``ready``（它认为该商品没有进行中任务）时通知一次。
   *
   * 用途：让父级**纠偏**一次商品状态 —— ready 往往意味着「任务早已结束，只是本页的流断了」，
   * 没有这个回调时 UI 会一直停在「生成中…」直到手动刷新。
   * 注意：不改变 ``sse.ts`` 的语义（``ready`` 仍然**不重连**，只是多通知一次父级刷新）。
   */
  onNoActiveTask?: () => void;
  height?: number;
}

// 阶段行文案不在本地实现：统一用共享层 `../services/streamLabels`（formatEvent / statusLine）。
// 这里曾经有一份"与移动端逐条对齐"的拷贝，注释还写着"改一侧必须改另一侧" —— 结果 `agent.tool`
// 的键名两边一起读错（生产者发 `tool`，两边都读 `name`），界面上工具名常年空白。
// **一份实现 + 用真实事件载荷断言**才是可靠的防再发方式。

/**
 * 生成过程的实时视图（打字机正文 / 阶段 / 配图）。
 *
 * 挂载即连流，卸载即 abort；命中终态或 `ready` 后连接器**不会自己复活** ——
 * 所以重新生成时必须由父组件换 `key` 重挂载（见 ProductDetailPage 的 `streamNonce`）。
 */
export default function StreamingDisplay({
  productId,
  reconnectDelayMs,
  onTerminal,
  onWaiting,
  onDone,
  onNoActiveTask,
  height = 280,
}: Props) {
  const [text, setText] = useState("");
  const [log, setLog] = useState<SseFrame[]>([]);
  const [liveImages, setLiveImages] = useState<LiveImage[]>([]);
  const [state, setState] = useState("连接中…");
  const [fatal, setFatal] = useState<string | null>(null);
  const [attemptSeed, setAttemptSeed] = useState(0); // 「重试」按钮：+1 触发重连
  const boxRef = useRef<HTMLPreElement | null>(null);
  const pinnedRef = useRef(true); // 用户没往上翻时自动滚到底
  const callbacks = useRef({ onTerminal, onWaiting, onDone, onNoActiveTask });
  callbacks.current = { onTerminal, onWaiting, onDone, onNoActiveTask };

  useEffect(() => {
    const ctrl = new AbortController();
    let waitingSignaled = false;

    const handleFrame = (frame: SseFrame) => {
      if (frame.comment) {
        // 注释帧：空闲关流/读流异常由连接器的回调处理状态文案，这里只记一行备查
        setLog((prev) => [...prev.slice(-199), frame]);
        return;
      }
      if (frame.type === "content.chunk") {
        const chunk = (frame.data ?? {}) as { text?: string };
        if (chunk.text) setText((prev) => prev + chunk.text);
        return;
      }
      if (frame.type === "image.ready") {
        // AI 配图就绪：直接渲染在打字机文本下方（图片无法「打字机」流式）
        const img = (frame.data ?? {}) as { url?: string; alt?: string };
        if (img.url) {
          setLiveImages((prev) => [...prev, { url: img.url as string, alt: img.alt }]);
          setState("✔ AI 配图已就绪，正在写入…");
        }
        return;
      }
      setLog((prev) => [...prev.slice(-199), frame]);
      if (frame.type === "hitl.waiting") {
        // PA：这是**终态**（服务端随即关流，等待审批可能持续数小时）。
        // 必须立刻脱离「生成中」并通知父级刷新商品状态；**不要**再等 approval.resumed
        // （审批通过后由父级按需重开流，见 pages/ProductDetailPage 的说明）。
        setState("✋ 已生成，等待人工审批中（可稍后刷新或到审批中心处理）");
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
      // 状态行只给业务短句（**不透出内部事件名**，如 `agent.tool`）
      setState(statusLine(frame));
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
        // ready = 服务端认为没有进行中任务：顺手让父级刷新一次商品状态做纠偏
        // （典型场景：任务其实已结束，只是本页的流断在了之前 —— 否则按钮会一直停着）。
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

  const stageLines = log.map((frame) => formatEvent(frame)).filter((line) => line.length > 0).join("\n");
  const hasBody = text.length > 0 || stageLines.length > 0;

  return (
    <div>
      <Space style={{ marginBottom: 8 }} wrap>
        <Typography.Text>
          流式状态：<b>{state}</b>
        </Typography.Text>
        {fatal ? (
          <Button
            size="small"
            onClick={() => {
              setFatal(null);
              setState("重新连接中…");
              setAttemptSeed((n) => n + 1);
            }}
          >
            重新连接
          </Button>
        ) : null}
      </Space>
      <pre
        ref={boxRef}
        onScroll={() => {
          const el = boxRef.current;
          if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        style={{ height, overflow: "auto", background: "#fafafa", padding: 8, fontSize: 12, whiteSpace: "pre-wrap" }}
      >
        {hasBody ? `${text}${text && stageLines ? "\n" : ""}${stageLines}` : "暂无事件"}
      </pre>
      {liveImages.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          {liveImages.map((img, i) => (
            <img
              key={`${img.url}-${i}`}
              src={img.url}
              alt={img.alt ?? "AI 配图"}
              style={{
                width: "100%",
                maxWidth: 640,
                height: "auto",
                display: "block",
                margin: "8px auto",
                borderRadius: 8,
              }}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

