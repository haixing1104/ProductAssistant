// AI 内容快照的只读展示小工具（审批中心详情 + 商品详情页「审批与驳回复盘」共用）。
import type { ContentBlock, ContentSnapshot } from "../types/api";

/** 从快照提取纯文本（兼容老数据：无 blocks → 空串，不抛错）。 */
export function snapshotText(s: ContentSnapshot | null | undefined): string {
  return (
    s?.content?.blocks
      ?.filter((b) => b.type === "text")
      .map((b) => b.text ?? "")
      .filter((t) => t.length > 0)
      .join(String.fromCharCode(10)) ?? ""
  );
}

/** 从快照取 blocks（供 BlockRenderer 图文渲染）。 */
export function snapshotBlocks(s: ContentSnapshot | null | undefined): ContentBlock[] {
  return s?.content?.blocks ?? [];
}

/** 转人工/驳回原因 → 中文标签与 Tag 颜色（与 backend `message.REASON_LABELS` 对齐）。 */
export function reasonMeta(reason: string | undefined): { label: string; color: string } | null {
  if (reason === "high_value") return { label: "高价商品（> ¥500），需人工放行", color: "gold" };
  if (reason === "quality_exhausted") return { label: "AI 内容多次未达标，转人工", color: "volcano" };
  return null;
}

/**
 * 转人工原因的**短标签**（表格列用）。
 *
 * 为什么需要（2026-09 反馈）: 列表里直接把上面那串长文案塞进 `<Tag>`，有色块 + 长文本把
 * 「评估分」那列压得几乎看不见（视觉层级失衡）。列表只需"是什么原因"，完整解释放 Tooltip。
 */
export function reasonShortLabel(reason: string | undefined): string {
  if (reason === "high_value") return "高价商品";
  if (reason === "quality_exhausted") return "质量未达标";
  return reason ?? "";
}

/** 评估分 → 颜色（≥90 绿 / ≥80 金 / <80 红；无分数返回 default）。 */
export function scoreColor(score: number | string | null | undefined): string {
  const value = typeof score === "string" ? Number(score) : score;
  if (value === null || value === undefined || Number.isNaN(value)) return "default";
  if (value >= 90) return "green";
  if (value >= 80) return "gold";
  return "red";
}

/** 审批快照里的「评估命中点」（真实快照用 violations；errors 是更早的形状，做读侧兼容）。 */
export function snapshotViolations(
  snapshot: ContentSnapshot | null | undefined,
): Array<{ keyword?: string; reason?: string; severity?: string }> {
  const evaluation = snapshot?.evaluation_result;
  const raw = evaluation?.violations ?? evaluation?.errors;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is { keyword?: string; reason?: string; severity?: string } =>
    typeof item === "object" && item !== null,
  );
}
