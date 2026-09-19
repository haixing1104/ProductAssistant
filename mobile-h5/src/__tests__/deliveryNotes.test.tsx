// 通知投递状态（移动版）的展示口径 —— 守护「已送达的行不许再报投递失败」。
//
// 事故形态（2026-09 实测）: outbox 行 `status=sent`（重试后已送达）但 payload 仍残留
// `payload.last_error` → 三端直接渲染 `note.error`，把已经自愈的网络抖动说成投递失败。
// 用户原话：「钉钉其实是收到了发出的消息」，而详情里还挂着 `NotificationSendError: …`。
// 本文件把「已送达 → 不显示原因 / 真失败 → 必须显示原因」这两条钉死。
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DeliveryNotes from "../components/DeliveryNotes";

const SENT_WITH_STALE_ERROR = [
  {
    channel: "dingtalk",
    status: "sent",
    retry_count: 1,
    error: "NotificationSendError: 钉钉投递网络异常：ConnectError('[Errno 101] Network is unreachable')",
  },
];

describe("投递状态展示（按 status 作用域）", () => {
  it("已送达（重试后成功）：说清「重试 1 次后成功」，且**不显示**失败原因", () => {
    render(<DeliveryNotes notes={SENT_WITH_STALE_ERROR} />);

    expect(screen.getByText(/钉钉 · 已发送（重试 1 次后成功）/)).toBeInTheDocument();
    // 残留的 last_error 不许再露出来（这是本次事故的回归断言）
    expect(screen.queryByText(/Network is unreachable/)).toBeNull();
    expect(screen.queryByText(/NotificationSendError/)).toBeNull();
  });

  it("真正失败（dlq）：原因必须显示（审批人据此区分「配错 webhook」与「网络抖」）", () => {
    render(
      <DeliveryNotes
        notes={[{ channel: "dingtalk", status: "dlq", retry_count: 3, error: "webhook 404" }]}
      />,
    );

    expect(screen.getByText(/钉钉 · 投递失败（已尝试 3 次）/)).toBeInTheDocument();
    expect(screen.getByText("webhook 404")).toBeInTheDocument();
  });

  it("未配置外部通知：给中性文案，不说「失败」", () => {
    render(<DeliveryNotes notes={[]} />);

    expect(screen.getByText("未配置外部通知")).toBeInTheDocument();
  });
});
