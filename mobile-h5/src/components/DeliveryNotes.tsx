// 通知投递状态（移动版）：替代桌面 `DeliverySummary`。
//
// 语义完全复用共享层 `@pa/core/services/notificationStatus`（渠道名 / 状态色 / 失败判定）。
// 移动端差异：桌面用 `Tooltip` 悬浮看失败原因，而**手机没有 hover** —— 改为点按 `Popover`
// 展开逐渠道详情（照抄桌面会让「投递失败」变成一个点不开的灰字）。
import { Popover, Tag } from "antd-mobile";

import { channelLabel, hasDeliveryFailure, notificationsSummary, statusMeta } from "@pa/core/services/notificationStatus";
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
      {notes.map((note, i) => (
        <div key={`${note.channel}-${i}`} style={{ marginBottom: 4 }}>
          <b>{channelLabel(note.channel)}</b>
          <span>：{statusMeta(note).label}</span>
          {note.retry_count ? <span>（已重试 {note.retry_count} 次）</span> : null}
          {note.next_retry_at ? <div>下次重试：{note.next_retry_at}</div> : null}
          {note.error ? <div style={{ color: "#ff3141" }}>原因：{note.error}</div> : null}
        </div>
      ))}
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
        return (
          <div key={`${note.channel}-${i}`} style={{ marginBottom: 6 }}>
            <Tag color={toTagColor(meta.color)}>
              {channelLabel(note.channel)} · {meta.label}
              {note.retry_count ? `（第 ${note.retry_count} 次）` : ""}
            </Tag>
            {note.error ? (
              <div style={{ color: "#ff3141", fontSize: 12, marginTop: 2 }} className="pa-pre-wrap">
                {note.error}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
