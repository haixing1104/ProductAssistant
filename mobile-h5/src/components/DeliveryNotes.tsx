// 通知投递状态（移动版）：替代桌面 `DeliverySummary`。
//
// 语义完全复用共享层 `@pa/core/services/notificationStatus`（渠道名 / 状态色 / 失败判定 /
// **当前有效错误 `currentError`**）。失败原因**必须**经 `currentError` 取：历史行里出现过
// 「已送达但 payload 仍残留 `last_error`」，直接渲染 `note.error` 会把已经自愈的抖动
// 说成投递失败（2026-09 实测事故）。次数文案也统一由 `statusMeta` 出，组件不再自己拼。
// 移动端差异：桌面用 `Tooltip` 悬浮看失败原因，而**手机没有 hover** —— 改为点按 `Popover`
// 展开逐渠道详情（照抄桌面会让「投递失败」变成一个点不开的灰字）。
import { Popover, Tag } from "antd-mobile";

import {
  channelLabel,
  currentError,
  hasDeliveryFailure,
  notificationsSummary,
  statusMeta,
} from "@pa/core/services/notificationStatus";
import type { ApprovalNotification } from "@pa/core/types/api";

import { toTagColor } from "@pa/core/services/mobileFormat";

interface Props {
  notes?: ApprovalNotification[];
  /** 紧凑（列表卡片）：一行摘要 + 有失败时红色角标 */
  compact?: boolean;
}

/** 投递状态（移动版）：手机没有 hover，失败原因改点按 `Popover` 展开。 */
export default function DeliveryNotes({ notes, compact = false }: Props) {
  if (!notes || notes.length === 0) {
    return <Tag fill="outline">未配置外部通知</Tag>;
  }
  const failed = hasDeliveryFailure(notes);
  const detail = (
    <div style={{ maxWidth: 240, fontSize: 12, lineHeight: 1.6 }}>
      {notes.map((note, i) => {
        const err = currentError(note);
        return (
          <div key={`${note.channel}-${i}`} style={{ marginBottom: 4 }}>
            <b>{channelLabel(note.channel)}</b>
            <span>：{statusMeta(note).label}</span>
            {note.next_retry_at ? <div>下次重试：{note.next_retry_at}</div> : null}
            {err ? <div style={{ color: "#ff3141" }}>原因：{err}</div> : null}
          </div>
        );
      })}
    </div>
  );
  if (compact) {
    // Popover 的孩子必须是单个元素；点击 Tag 展开详情
    return (
      <Popover content={detail} trigger="click" placement="top" mode="dark">
        <Tag color={failed ? "danger" : toTagColor(statusMeta(notes[0]).color)}>
          {notificationsSummary(notes)}
        </Tag>
      </Popover>
    );
  }
  return (
    <div style={{ padding: "4px 0" }}>
      {notes.map((note, i) => {
        const meta = statusMeta(note);
        const err = currentError(note);
        return (
          <div key={`${note.channel}-${i}`} style={{ marginBottom: 6 }}>
            {/* 重试次数已由 statusMeta 的文案表达（重试中（第 N 次）/ 已发送（重试 N 次后成功）），
                这里不再自己拼后缀 —— 否则会出现「重试中（第 1 次）（第 1 次）」。 */}
            <Tag color={toTagColor(meta.color)}>
              {channelLabel(note.channel)} · {meta.label}
            </Tag>
            {err ? (
              <div style={{ color: "#ff3141", fontSize: 12, marginTop: 2 }} className="pa-pre-wrap">
                {err}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
