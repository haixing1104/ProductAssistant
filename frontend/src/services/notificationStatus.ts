// 审批通知渠道/投递状态 → 中文展示（审批列表「通知」列与详情抽屉共用）。
//
// PA 与 PP 的差异（重要）：PA 的 `notification_outbox.payload` **没有 `kind` 字段**，
// 后端按 `payload->>'approval_id'` join（见 `services/notifications/reader.py`）。
// 前端只需认字段，但**不要**假设存在 `kind`。
import type { ApprovalNotification } from "../types/api";

/** 渠道枚举 → 中文（与 backend `notification_outbox.channel` 对齐）。 */
const CHANNEL_LABELS: Record<string, string> = {
  email: "邮件",
  feishu: "飞书",
  dingtalk: "钉钉",
};

/** 渠道 → 中文名（未知渠道原样透出，便于发现后端新增通道）。 */
export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel;
}

/**
 * status + retry_count → 展示文案与颜色。
 *
 * 语义（与 backend deliverer 的状态机一致）:
 *   `sent` 已发送 / `dlq` 投递失败（需人工介入）/ `pending` + retry>0 = 重试中 / `pending` = 待投递。
 * 注意：`dlq` 是**终态**（达到 max_attempts），靠补投守护或人工处理，不会自己恢复。
 * `sent` + `retry_count > 0` = **首投失败、重试已送达** —— 必须说出来：否则「抖动过」与
 * 「一次成功」在界面上毫无区别，运维就丢掉了「这个渠道在抖」的唯一信号。
 */
export function statusMeta(n: ApprovalNotification): { label: string; color: string } {
  if (n.status === "sent") {
    return (n.retry_count ?? 0) > 0
      ? { label: `已发送（重试 ${n.retry_count} 次后成功）`, color: "green" }
      : { label: "已发送", color: "green" };
  }
  if (n.status === "dlq") {
    return (n.retry_count ?? 0) > 0
      ? { label: `投递失败（已尝试 ${n.retry_count} 次）`, color: "red" }
      : { label: "投递失败", color: "red" };
  }
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

/**
 * 当前仍然成立的失败原因（没有则 null）—— **三端渲染失败原因都必须走这里**。
 *
 * 为什么要这层判定（2026-09 实测事故）:
 *   `payload['last_error']` 是「当前故障」诊断字段（见 backend `deliverer`）。历史行里出现过
 *   「`status=sent` + 残留 `last_error`」，三端若直接渲染 `note.error`，一条**已送达**的通知
 *   看起来就是投递失败 —— 用户看到的正是「钉钉其实收到了，界面还在报 NotificationSendError」，
 *   而 `last_error` 是**不会被重试覆盖**的，所以那个红字会一直挂着。
 * 口径:
 *   · `sent`（含重试后成功）→ null（原因已失效，历史由 `retry_count` 表达）；
 *   · 其它状态（`pending` / `dlq` / 未知）→ 原样透出（未知状态宁可显示，不静默吞掉）。
 * 说明: 后端本轮已在送达时清理该字段，这里再判一次是为了**兼容尚未清理的历史行**
 *       （界面不该依赖"库已经被洗干净"）。
 */
export function currentError(n: ApprovalNotification): string | null {
  if (n.status === "sent") return null;
  return n.error ?? null;
}
