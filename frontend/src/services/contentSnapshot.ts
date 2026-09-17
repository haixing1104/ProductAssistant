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
