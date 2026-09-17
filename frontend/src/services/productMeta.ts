// 商品字典/标签（与 backend `schemas/api.STOCK_STATUS_VALUES` 和 DB CHECK 保持一致）。
//
// PA 与 PP 的枚举差异（照抄 PP 会漏）：
//   · `stock_status` 多一个 `preorder`（预售）；
//   · `status` 多一个 `deleted`（**历史数据**专用：软删功能已按业务决定移除，
//     现在删除只有「彻底删除」一条路径；`archived` 同理暂无写入方）。
export const STOCK_STATUS_LABELS: Record<string, string> = {
  in_stock: "有货",
  low_stock: "低库存",
  out_of_stock: "缺货",
  preorder: "预售",
};

export function stockStatusLabel(value?: string | null): string {
  if (!value) return "";
  return STOCK_STATUS_LABELS[value] ?? value;
}

/** 商品生命周期状态（draft → generating → waiting_approval → published；archived/deleted 为历史态）。 */
export const PRODUCT_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  generating: "生成中",
  waiting_approval: "待审批",
  published: "已上架",
  archived: "已归档",
  deleted: "已删除（历史）",
};

export function productStatusLabel(value?: string | null): string {
  if (!value) return "";
  return PRODUCT_STATUS_LABELS[value] ?? value;
}

/** Tag 颜色：让「进行中」与「终态」一眼可分辨（列表扫描用）。 */
export const PRODUCT_STATUS_COLORS: Record<string, string> = {
  draft: "default",
  generating: "processing",
  waiting_approval: "warning",
  published: "success",
  archived: "default",
  deleted: "error",
};

export function productStatusColor(value?: string | null): string {
  if (!value) return "default";
  return PRODUCT_STATUS_COLORS[value] ?? "default";
}

/** 商品状态筛选下拉项（列表页用；`deleted` 只给 admin，见页面注释）。 */
export const PRODUCT_STATUS_FILTERS = [
  { value: "draft", label: "草稿" },
  { value: "generating", label: "生成中" },
  { value: "waiting_approval", label: "待审批" },
  { value: "published", label: "已上架" },
  { value: "archived", label: "已归档" },
];

/** 库存下拉项（表单用）。 */
export const STOCK_OPTIONS = Object.entries(STOCK_STATUS_LABELS).map(([value, label]) => ({ value, label }));

/** 审批状态标签（pending / approved / rejected）。 */
export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已驳回",
};

export function approvalStatusLabel(value?: string | null): string {
  if (!value) return "";
  return APPROVAL_STATUS_LABELS[value] ?? value;
}

/** 合规严重级标签（high/medium 阻断，low 仅提示；与 ai-engine BLOCKING_SEVERITIES 对齐）。 */
export const SEVERITY_LABELS: Record<string, string> = {
  high: "高（阻断）",
  medium: "中（阻断）",
  low: "低（提示）",
};

export const SEVERITY_COLORS: Record<string, string> = {
  high: "red",
  medium: "orange",
  low: "blue",
};

export function severityLabel(value?: string | null): string {
  if (!value) return "";
  return SEVERITY_LABELS[value] ?? value;
}

export function severityColor(value?: string | null): string {
  if (!value) return "default";
  return SEVERITY_COLORS[value] ?? "default";
}
