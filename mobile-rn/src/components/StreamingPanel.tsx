// SSE 实时展示（RN 版）—— 替代 H5 的 `StreamingPanel`。
//
// **语义逐条照搬**（它承载的是 PA 与 PP 的三个关键差异，照抄 PP 会错 —— 见 @pa/core/services/sse 顶部注释）:
//   1) `hitl.waiting` 是**终态**：收到即停重连、显示「已转人工审批」，**不要**等 `approval.resumed`；
//   2) `ready` 是服务端控制帧（它认为没有进行中任务）：停止 + 通知父级刷新纠偏，不空转重连；
//   3) 注释帧 `: stream-idle-close` / `: stream-error` 不触发 onmessage → 由连接器回调转成状态文案。
// 而且**连接器本身（含重连退避、Last-Event-ID 续传）就是共享层那一份** —— 这里只做展示。
//
// 移动端差异: 正文区固定高度 + 自动滚底（用户往上翻时不打断）；配图缩略图点开看大图。
import { useEffect, useRef, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";

import Button from "../ui/Button";
import { ImageThumbs } from "../ui/ImageViewer";
import { PreWrapText } from "../ui/List";
import Tag from "../ui/Tag";
import { colors, font, radius, space } from "../ui/theme";
import { connectProductStream, type SseFrame } from "@pa/core/services/sse";
import { formatEvent } from "@pa/core/services/streamLabels";

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
 * 实时生成面板（RN 版）：打字机正文 + 阶段 + 配图。
 *
 * 语义与 H5/桌面端**逐条一致**（`hitl.waiting` 是终态 / `ready` 不重连 / 注释帧三态），
 * 差异只在渲染：正文固定高度 + 自动滚底（用户往上翻时不打断）、配图点开放大。
 */
export default function StreamingPanel({
  productId,
  reconnectDelayMs,
  onTerminal,
  onWaiting,
  onDone,
  onNoActiveTask,
  height = 220,
}: Props) {
  const [text, setText] = useState("");
  const [log, setLog] = useState<SseFrame[]>([]);
  const [liveImages, setLiveImages] = useState<string[]>([]);
  const [status, setStatus] = useState("连接中…");
  const [fatal, setFatal] = useState<string | null>(null);
  const [attemptSeed, setAttemptSeed] = useState(0); // 「重新连接」按钮：+1 触发重连
  const scrollRef = useRef<ScrollView | null>(null);
  const pinnedRef = useRef(true); // 用户没往上翻时自动滚到底
  // 回调放 ref：避免父级每次渲染都重连（连接只该由 productId / attemptSeed 变化触发）
  const callbacks = useRef({ onTerminal, onWaiting, onDone, onNoActiveTask });
  callbacks.current = { onTerminal, onWaiting, onDone, onNoActiveTask };

  useEffect(() => {
    // RN 自带 AbortController（expo 的 winter runtime 还会再补一层 AbortSignal 兼容）
    const ctrl = new AbortController();
    let waitingSignaled = false;

    const handleFrame = (frame: SseFrame) => {
      if (frame.comment) {
        setLog((prev) => [...prev.slice(-199), frame]);
        return;
      }
      if (frame.type === "content.chunk") {
        const chunk = (frame.data ?? {}) as { text?: string };
        if (chunk.text) setText((prev) => prev + chunk.text);
        return;
      }
      if (frame.type === "image.ready") {
        const image = (frame.data ?? {}) as { url?: string };
        if (image.url) {
          setLiveImages((prev) => [...prev, image.url as string]);
          setStatus("✔ AI 配图已就绪，正在写入…");
        }
        return;
      }
      setLog((prev) => [...prev.slice(-199), frame]);
      if (frame.type === "hitl.waiting") {
        // PA：这是终态（服务端随即关流；等待审批可能持续数小时）—— 不要再等 approval.resumed
        setStatus("✋ 已生成，等待人工审批中（可在「审批」页处理）");
        if (!waitingSignaled) {
          waitingSignaled = true;
          callbacks.current.onWaiting?.();
        }
        return;
      }
      if (frame.type === "done") {
        setStatus("生成完成");
        callbacks.current.onDone?.();
        return;
      }
      setStatus(`阶段：${frame.type ?? "raw"}`);
    };

    void connectProductStream({
      productId,
      signal: ctrl.signal,
      retryDelayMs: reconnectDelayMs,
      onFrame: handleFrame,
      onIdle: () => setStatus("空闲（服务端已关流），自动续连中…"),
      onTransientError: (reason) => setStatus(`连接中断：${reason}，自动重试中…`),
      onReady: () => {
        setStatus("（暂无进行中任务）");
        // ready = 服务端认为没有进行中任务：让父级刷新一次做**状态纠偏**
        callbacks.current.onNoActiveTask?.();
      },
      onTerminal: (type) => {
        if (type === "hitl.waiting") setStatus("✋ 已生成，等待人工审批中");
        else if (type === "done") setStatus("生成完成");
        else if (type === "rejected") setStatus("已被驳回（商品已退回草稿）");
        else setStatus("生成失败（商品已退回草稿，请查看轨迹后重试）");
        callbacks.current.onTerminal?.(type);
      },
      onFatal: (reason) => {
        setStatus(`连接失败：${reason}`);
        setFatal(reason);
        callbacks.current.onTerminal?.("failed");
      },
    });

    return () => ctrl.abort();
    // attemptSeed 用于「重新连接」按钮强制重连（连接器一旦 stopped 不会自己复活）
  }, [productId, attemptSeed, reconnectDelayMs]);

  const stageLines = log
    .map((frame) => formatEvent(frame))
    .filter((line) => line.length > 0)
    .join("\n");
  const body =
    text || stageLines ? `${text}${text && stageLines ? "\n" : ""}${stageLines}` : "暂无事件（等待服务端推送…）";

  return (
    <View>
      <View style={styles.header}>
        <Tag color={fatal ? "danger" : "primary"}>{fatal ? "已断开" : "实时"}</Tag>
        <PreWrapText style={styles.status}>{status}</PreWrapText>
        {fatal ? (
          <Button
            size="mini"
            variant="primary"
            onPress={() => {
              setFatal(null);
              setStatus("重新连接中…");
              setAttemptSeed((value) => value + 1);
            }}
            testID="pa-stream-reconnect"
          >
            重新连接
          </Button>
        ) : null}
      </View>
      <ScrollView
        ref={scrollRef}
        style={[styles.box, { height }]}
        onScroll={(event) => {
          const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
          pinnedRef.current = contentSize.height - contentOffset.y - layoutMeasurement.height < 24;
        }}
        scrollEventThrottle={16}
        onContentSizeChange={() => {
          if (pinnedRef.current) scrollRef.current?.scrollToEnd({ animated: false });
        }}
        testID="pa-stream-box"
      >
        <PreWrapText style={styles.body}>{body}</PreWrapText>
      </ScrollView>
      {liveImages.length > 0 ? <ImageThumbs urls={liveImages} size={96} testID="pa-stream-images" /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  header: { flexDirection: "row", alignItems: "center", gap: space.sm, marginBottom: space.sm },
  status: { flex: 1, fontSize: font.sm, color: colors.textSecondary },
  box: {
    backgroundColor: colors.bgMuted,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  body: { fontSize: font.sm, lineHeight: 22 },
});

