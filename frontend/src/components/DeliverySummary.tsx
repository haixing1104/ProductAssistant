// 通知投递状态摘要（审批列表「通知」列 + 详情抽屉共用；与 backend reader 的字段对齐）。
import { Space, Tag, Tooltip, Typography } from "antd";

import { channelLabel, statusMeta } from "../services/notificationStatus";
import type { ApprovalNotification } from "../types/api";

interface Props {
  notes?: ApprovalNotification[];
  /** 紧凑模式（表格列）：只给 Tag；非紧凑再补一行失败原因 */
  compact?: boolean;
}

/** 各渠道投递状态（紧凑模式只给 Tag + Tooltip：表格列宽有限，失败原因不占位）。 */
export default function DeliverySummary({ notes, compact = false }: Props) {
  if (!notes || notes.length === 0) {
    return <Typography.Text type="secondary">未配置外部通知</Typography.Text>;
  }
  return (
    <Space direction={compact ? "horizontal" : "vertical"} size={4} wrap>
      {notes.map((note, i) => {
        const meta = statusMeta(note);
        const tip = note.error
          ? `最后失败原因：${note.error}`
          : note.next_retry_at
            ? `下次重试：${note.next_retry_at}`
            : undefined;
        const tag = (
          <Tag key={`${note.channel}-${i}`} color={meta.color}>
            {channelLabel(note.channel)} · {meta.label}
          </Tag>
        );
        return compact ? (
          <Tooltip key={`${note.channel}-${i}`} title={tip}>
            {tag}
          </Tooltip>
        ) : (
          <div key={`${note.channel}-${i}`}>
            {tag}
            {note.error ? (
              <Typography.Text type="danger" style={{ marginLeft: 8, fontSize: 12 }}>
                {note.error}
              </Typography.Text>
            ) : null}
          </div>
        );
      })}
    </Space>
  );
}
