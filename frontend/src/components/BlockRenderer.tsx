// 图文详情通用渲染器：把 `content_data.blocks` / `content_snapshot.blocks` 渲染成电商详情观感。
//
// 三个消费方共用：商品详情「最新内容」、审批中心「AI 生成详情」、商品详情「审批与驳回复盘」。
// 兼容性：老数据只有 text block / blocks 为空 → 退化为纯文本展示，不抛错、不空白。
import { Typography } from "antd";

import type { ContentBlock } from "../types/api";

interface Props {
  blocks?: ContentBlock[] | null;
  /** 内容区最大高度（超出滚动）；不传则自然撑开 */
  maxHeight?: number;
  /** 图片圆角/描边，适配不同容器底色 */
  bordered?: boolean;
}

export function blocksToPlainText(blocks?: ContentBlock[] | null): string {
  return (
    blocks
      ?.filter((b) => b.type === "text")
      .map((b) => b.text ?? "")
      .filter((s) => s.length > 0)
      .join("\n") ?? ""
  );
}

export default function BlockRenderer({ blocks, maxHeight, bordered = false }: Props) {
  const list = blocks ?? [];
  const hasBlocks = list.length > 0;

  const inner =
    list.length === 0 ? (
      <Typography.Text type="secondary">（无内容）</Typography.Text>
    ) : (
      list.map((block, i) => {
        if (block.type === "image" && block.url) {
          return (
            <div key={i} style={{ margin: "12px 0", textAlign: "center" }}>
              <img
                src={block.url}
                alt={block.alt ?? "商品图"}
                loading="lazy"
                style={{
                  width: "100%",
                  maxWidth: 640,
                  height: "auto",
                  borderRadius: bordered ? 8 : undefined,
                  border: bordered ? "1px solid #f0f0f0" : undefined,
                  display: "block",
                  margin: "0 auto",
                }}
              />
            </div>
          );
        }
        const text = typeof block.text === "string" && block.text ? block.text : "";
        return (
          <Typography.Paragraph
            key={i}
            style={{ whiteSpace: "pre-wrap", marginBottom: 12, fontSize: 14, lineHeight: 1.8 }}
          >
            {text}
          </Typography.Paragraph>
        );
      })
    );

  return (
    <div
      style={{
        maxHeight: hasBlocks ? maxHeight ?? undefined : undefined,
        overflowY: maxHeight && hasBlocks ? "auto" : undefined,
        background: "#fff",
      }}
    >
      {inner}
    </div>
  );
}
