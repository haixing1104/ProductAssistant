// 状态/评分 Tag —— 与 H5 的 `components/StatusTag.tsx` 逐条对齐。
//
// 颜色口径**全部来自共享核心层**（`@pa/core/services/{productMeta,contentSnapshot}`）：
//   · 状态/审批状态：中文名 + 语义色；
//   · 评估分：≥90 绿 / ≥80 金 / <80 红（审批列表的扫视锚点，必须与 H5 一致）。
import Tag from "../ui/Tag";
import { scoreColor } from "@pa/core/services/contentSnapshot";
import { approvalStatusLabel, productStatusColor, productStatusLabel } from "@pa/core/services/productMeta";
import { toTagColor } from "@pa/core/services/mobileFormat";

/** 商品状态 Tag（颜色经 `toTagColor` 翻译，与 H5/桌面端同源同义）。 */
export function ProductStatusTag({ status }: { status?: string | null }) {
  if (!status) return null;
  return <Tag color={toTagColor(productStatusColor(status))}>{productStatusLabel(status)}</Tag>;
}

/** 审批状态：pending 金 / approved 绿 / rejected 红（与桌面端列渲染一致）。 */
export function ApprovalStatusTag({ status }: { status?: string | null }) {
  if (!status) return null;
  const color = status === "approved" ? "success" : status === "rejected" ? "danger" : "warning";
  return <Tag color={color}>{approvalStatusLabel(status)}</Tag>;
}

/** 评估分（实心）：审批列表必须一眼可见（2026-09 反馈）。 */
export function ScoreTag({ score }: { score?: number | string | null }) {
  return (
    <Tag color={toTagColor(scoreColor(score))} fill="solid" style={{ minWidth: 40, alignItems: "center" }}>
      {score ?? "-"}
    </Tag>
  );
}

/** 合规严重级（high 红 / medium 金 / low 蓝）。 */
export function SeverityTag({ severity, label }: { severity?: string | null; label: string }) {
  if (!severity) return null;
  const color = severity === "high" ? "danger" : severity === "medium" ? "warning" : "primary";
  return <Tag color={color}>{label}</Tag>;
}
