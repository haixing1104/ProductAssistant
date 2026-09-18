// 图文内容渲染 —— 与 H5 的 `components/BlockView.tsx` 同口径（替代桌面 `BlockRenderer`）。
//
// 与桌面端的差异: 桌面 `<img>` 直接展示；手机端图片点开要能**看细节**（AI 配图质量是审核判断依据之一），
// 所以接 `ImageThumbs`/`ImageViewer`；正文保留换行（AI 正文里的分段信息不能丢）。
import { StyleSheet, View } from "react-native";

import { ImageThumbs } from "../ui/ImageViewer";
import { PreWrapText } from "../ui/List";
import { space } from "../ui/theme";
import type { ContentBlock } from "@pa/core/types/api";

interface Props {
  blocks?: ContentBlock[] | null;
  emptyText?: string;
}

export default function BlockView({ blocks, emptyText = "（无内容）" }: Props) {
  const list = blocks ?? [];
  if (list.length === 0) {
    return <PreWrapText style={styles.empty}>{emptyText}</PreWrapText>;
  }
  // 文本块连续显示、图片块统一收集：手机窄屏上"图挨着图"比"图插在段落中间"更好读
  const images = list.filter((block) => block.type === "image" && block.url).map((block) => block.url as string);

  return (
    <View>
      {list.map((block, index) => {
        if (block.type === "image") return null;
        const text = typeof block.text === "string" && block.text ? block.text : "";
        if (!text) return null;
        return (
          <PreWrapText key={`text-${index}`} style={styles.paragraph}>
            {text}
          </PreWrapText>
        );
      })}
      {images.length > 0 ? <ImageThumbs urls={images} size={96} testID="pa-block-images" /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  paragraph: { marginVertical: space.xs, lineHeight: 24 },
  empty: { fontSize: 13, color: "#999" },
});
