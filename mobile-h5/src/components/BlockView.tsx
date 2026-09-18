// 图文内容渲染（移动版）：替代桌面 `BlockRenderer`。
//
// 与桌面端的差异: 桌面用 `<img>` 直接展示，移动端图片点开要能**放大看细节**（商品主图/配图的
// 真实使用场景），所以接 antd-mobile 的 `ImageViewer`；正文用 `.pa-pre-wrap` 保留换行。
import { Image, ImageViewer, List } from "antd-mobile";
import { useState } from "react";

import type { ContentBlock } from "@pa/core/types/api";

interface Props {
  blocks?: ContentBlock[] | null;
  emptyText?: string;
}

/** 图文渲染（移动版）：图片点开可放大（`ImageViewer`），正文用 `.pa-pre-wrap` 保留换行。 */
export default function BlockView({ blocks, emptyText = "（无内容）" }: Props) {
  const [preview, setPreview] = useState<string | null>(null);
  const list = blocks ?? [];
  if (list.length === 0) {
    return (
      <List>
        <List.Item>{emptyText}</List.Item>
      </List>
    );
  }
  return (
    <div style={{ padding: "4px 0" }}>
      {list.map((block, i) => {
        if (block.type === "image" && block.url) {
          return (
            <div key={`img-${i}`} style={{ margin: "12px 0", textAlign: "center" }}>
              <Image
                src={block.url}
                alt={block.alt ?? "商品图"}
                fit="contain"
                width="100%"
                lazy
                onClick={() => setPreview(block.url ?? null)}
                style={{ borderRadius: 8, border: "1px solid #f0f0f0" }}
              />
            </div>
          );
        }
        const text = typeof block.text === "string" && block.text ? block.text : "";
        if (!text) return null;
        return (
          <p key={`text-${i}`} className="pa-pre-wrap" style={{ fontSize: 15, lineHeight: 1.8, margin: "8px 0" }}>
            {text}
          </p>
        );
      })}
      {/* 点图放大：移动端必须能看清配图细节（AI 配图质量是审核判断依据之一） */}
      <ImageViewer image={preview ?? ""} visible={preview !== null} onClose={() => setPreview(null)} />
    </div>
  );
}
