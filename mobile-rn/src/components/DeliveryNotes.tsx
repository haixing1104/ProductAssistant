// 通知投递状态 —— 与 H5 的 `components/DeliveryNotes.tsx` 同口径。
//
// 语义全部复用共享层 `@pa/core/services/notificationStatus`（渠道名 / 状态色 / 失败判定 /
// **当前有效错误 `currentError`**）。失败原因**必须**经 `currentError` 取：历史行里出现过
// 「已送达但 payload 仍残留 `last_error`」，直接渲染 `note.error` 会把已经自愈的抖动
// 说成投递失败（2026-09 实测事故）。次数文案统一由 `statusMeta` 出，组件不再自己拼。
// **手机差异（照抄桌面会错）**: 桌面用 `Tooltip` 悬浮看失败原因，而手机没有 hover ——
// 这里改成"点按 Tag 展开详情"（展开后逐渠道列出重试次数与失败原因，`dlq` 是终态要点明）。
import { useState } from "react";
import { Pressable, StyleSheet, View } from "react-native";

import Tag from "../ui/Tag";
import { PreWrapText } from "../ui/List";
import { colors, font, space } from "../ui/theme";
import { toTagColor } from "@pa/core/services/mobileFormat";
import {
  channelLabel,
  currentError,
  hasDeliveryFailure,
  notificationsSummary,
  statusMeta,
} from "@pa/core/services/notificationStatus";
import type { ApprovalNotification } from "@pa/core/types/api";

interface Props {
  notes?: ApprovalNotification[];
  /** 紧凑（列表卡片）：一行摘要 + 有失败时红色 + 点按展开 */
  compact?: boolean;
}

/** 投递状态：手机没有 hover，失败原因改为点按展开（`dlq` 是终态，文案要点明需人工介入）。 */
export default function DeliveryNotes({ notes, compact = false }: Props) {
  const [open, setOpen] = useState(false);

  if (!notes || notes.length === 0) {
    return <Tag>未配置外部通知</Tag>;
  }
  const failed = hasDeliveryFailure(notes);

  const detail = (
    <View style={styles.detail}>
      {notes.map((note, index) => {
        const meta = statusMeta(note);
        const err = currentError(note);
        return (
          <View key={`${note.channel}-${index}`} style={styles.detailRow}>
            {/* 重试次数由 statusMeta 的文案表达，这里不再拼后缀（否则会出现「第 1 次）（第 1 次）」） */}
            <Tag color={toTagColor(meta.color)}>
              {channelLabel(note.channel)} · {meta.label}
            </Tag>
            {note.next_retry_at ? (
              <PreWrapText style={styles.dim}>下次重试：{note.next_retry_at}</PreWrapText>
            ) : null}
            {err ? <PreWrapText style={styles.error}>原因：{err}</PreWrapText> : null}
          </View>
        );
      })}
      <PreWrapText style={styles.dim}>
        投递失败（红）多为 webhook/凭据问题或对端不可用；`dlq` 是终态，需人工修配置后由补投守护或运维处理。
      </PreWrapText>
    </View>
  );

  if (!compact) {
    return detail;
  }
  return (
    <View>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="查看通知投递详情"
        onPress={() => setOpen((value) => !value)}
        hitSlop={{ top: 6, bottom: 6, left: 2, right: 2 }}
      >
        <Tag color={failed ? "danger" : toTagColor(statusMeta(notes[0]).color)}>
          {notificationsSummary(notes)}
          {open ? " ▲" : " ▼"}
        </Tag>
      </Pressable>
      {open ? detail : null}
    </View>
  );
}

const styles = StyleSheet.create({
  detail: { marginTop: space.sm, gap: space.xs },
  detailRow: { marginBottom: space.xs },
  dim: { fontSize: font.xs, color: colors.textSecondary },
  error: { fontSize: font.xs, color: colors.danger },
});
