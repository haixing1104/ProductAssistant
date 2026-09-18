// 图片查看（全屏 Modal + contain）。
//
// 与 H5 的 `ImageViewer` 的差异（**要说清楚，不能假装一样**）:
//   首版是"全屏看整图"，**没有双指缩放** —— 双端一致的手势缩放需要 gesture-handler/reanimated
//   这类原生依赖，收益（多数 AI 配图本身分辨率足够）低于成本。若后续确需缩放，替换点只在本文件。
import { useState } from "react";
import { Image, Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, font, space } from "./theme";

/** 单图查看（点击全屏、点任意处关闭）。 */
export function ImageViewer({
  image,
  visible,
  onClose,
}: {
  image: string;
  visible: boolean;
  onClose: () => void;
}) {
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose} accessibilityLabel="关闭图片">
        {image ? <Image source={{ uri: image }} style={styles.image} resizeMode="contain" /> : null}
      </Pressable>
    </Modal>
  );
}

/** 一组缩略图（商品图素材 / AI 配图）：点开看大图。 */
export function ImageThumbs({
  urls,
  size = 88,
  testID,
}: {
  urls: string[];
  size?: number;
  testID?: string;
}) {
  const [preview, setPreview] = useState<string | null>(null);
  return (
    <View style={styles.thumbs} testID={testID}>
      {urls.map((url, index) => (
        <Pressable
          key={`${url}-${index}`}
          accessibilityRole="imagebutton"
          accessibilityLabel={`查看第 ${index + 1} 张图`}
          onPress={() => setPreview(url)}
          style={({ pressed }) => ({ opacity: pressed ? 0.8 : 1 })}
        >
          <Image source={{ uri: url }} style={{ width: size, height: size, borderRadius: 6 }} resizeMode="cover" />
        </Pressable>
      ))}
      {urls.length === 0 ? <Text style={styles.empty}>（暂无图片）</Text> : null}
      <ImageViewer image={preview ?? ""} visible={preview !== null} onClose={() => setPreview(null)} />
    </View>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.92)", alignItems: "center", justifyContent: "center" },
  image: { width: "100%", height: "80%" },
  thumbs: { flexDirection: "row", flexWrap: "wrap", gap: space.sm, marginTop: space.sm },
  empty: { fontSize: font.sm, color: colors.textSecondary },
});
