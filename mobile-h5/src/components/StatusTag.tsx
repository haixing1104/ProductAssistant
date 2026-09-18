// 状态/评分的移动端 Tag（表格 → 卡片 后，Tag 的颜色成为"扫视找异常"的唯一锚点，必须统一）。
//
// 颜色口径全部来自共享核心层（@pa/core/services/productMeta、contentSnapshot），
// 再经 services/format.toTagColor 翻成 antd-mobile 认的预设色 —— 保证与桌面端**同源同义**。
import { Tag } from "antd-mobile";

import { scoreColor } from "@pa/core/services/contentSnapshot";
import { approvalStatusLabel, productStatusColor, productStatusLabel } from "@pa/core/services/productMeta";

import { toTagColor } from "@pa/core/services/mobileFormat";

export function ProductStatusTag({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <Tag color={toTagColor(productStatusColor(status))} fill="outline">
      {productStatusLabel(status)}
    </Tag>
  );
}

/** 审批状态：pending 金 / approved 绿 / rejected 红（与桌面端列渲染一致）。 */
export function ApprovalStatusTag({ status }: { status?: string | null }) {
  if (!status) return null;
  const color = status === "approved" ? "success" : status === "rejected" ? "danger" : "warning";
  return <Tag color={color}>{approvalStatusLabel(status)}</Tag>;
}

/** 评估分（实心）：≥90 绿 / ≥80 金 / <80 红 —— 审批列表必须一眼可见（2026-09 反馈）。 */
export function ScoreTag({ score }: { score?: number | string | null }) {
  return (
    <Tag color={toTagColor(scoreColor(score))} style={{ minWidth: 40, textAlign: "center" }}>
      {score ?? "-"}
    </Tag>
  );
}

/** 合规严重级（high 红 / medium 金 / low 蓝）。 */
export function SeverityTag({ severity, label }: { severity?: string | null; label: string }) {
  if (!severity) return null;
  const color = severity === "high" ? "danger" : severity === "medium" ? "warning" : "primary";
  return (
    <Tag color={color} fill="outline">
      {label}
    </Tag>
  );
}
