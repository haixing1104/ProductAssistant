// 审批通知渠道/投递状态 → 中文展示（审批列表「通知」列与详情抽屉共用）。
//
// PA 与 PP 的差异（重要）：PA 的 `notification_outbox.payload` **没有 `kind` 字段**，
// 后端按 `payload->>'approval_id'` join（见 `services/notifications/reader.py`）。
// 前端只需认字段，但**不要**假设存在 `kind`。
import type { ApprovalNotification } from "../types/api";

const CHANNEL_LABELS: Record<string, string> = {
  email: "邮件",
  feishu: "飞书",
  dingtalk: "钉钉",
};

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel;
}

/**
 * status + retry_count → 展示文案与颜色。
 *
 * 语义（与 backend deliverer 的状态机一致）:
 *   `sent` 已发送 / `dlq` 投递失败（需人工介入）/ `pending` + retry>0 = 重试中 / `pending` = 待投递。
 * 注意：`dlq` 是**终态**（达到 max_attempts），靠补投守护或人工处理，不会自己恢复。
 */
export function statusMeta(n: ApprovalNotification): { label: string; color: string } {
  if (n.status === "sent") return { label: "已发送", color: "green" };
  if (n.status === "dlq") return { label: "投递失败", color: "red" };
  if (n.status === "pending") {
    return (n.retry_count ?? 0) > 0
      ? { label: `重试中（第 ${n.retry_count} 次）`, color: "orange" }
      : { label: "待投递", color: "blue" };
  }
  return { label: n.status, color: "default" };
}

/** 一行摘要文案：`钉钉 已发送 / 飞书 投递失败`（供表格列紧凑展示）。 */
export function notificationsSummary(notes: ApprovalNotification[] | undefined): string {
  if (!notes || notes.length === 0) return "未配置外部通知";
  return notes.map((n) => `${channelLabel(n.channel)} ${statusMeta(n).label}`).join("；");
}

/** 是否存在投递失败（用于加红色角标，让运维一眼看到）。 */
export function hasDeliveryFailure(notes: ApprovalNotification[] | undefined): boolean {
  return (notes ?? []).some((n) => n.status === "dlq");
}
