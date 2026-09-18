// 选择本地文件 / 图片（RN 专属能力，不属于平台端口的 6 个点，因此单独放一个文件）。
//
// 与 H5 的差异（照抄 H5 会错）:
//   H5 用 `<input type=file>`；RN 必须走系统选择器，且返回的是 `{uri, name, type}` 而不是 `File`。
//   这两个函数就把系统选择器的返回值"翻译"成共享层 `api/index.ts` 能接受的 `PaUploadFile`。
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";

import type { PaUploadFile } from "@pa/core/services/platform";

/** 与 H5 的 `IMAGE_TYPES` 同口径（后端 presign 只接受这三种）。 */
export const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

/** 选一个 CSV（商品批量导入）。取消返回 null。 */
export async function pickCsvFile(): Promise<PaUploadFile | null> {
  const result = await DocumentPicker.getDocumentAsync({
    // 部分 Android 提供方给不出 `text/csv`，放宽到 */* 由后端按内容判定（后端只认 CSV 表头）
    type: ["text/csv", "text/comma-separated-values", "application/vnd.ms-excel", "*/*"],
    copyToCacheDirectory: true,
    multiple: false,
  });
  if (result.canceled || result.assets.length === 0) return null;
  const asset = result.assets[0];
  return {
    uri: asset.uri,
    name: asset.name || "products.csv",
    type: asset.mimeType || "text/csv",
  };
}

export interface PickedImage {
  file: PaUploadFile;
  /** 本地文件字节数（用于上传前判 `max_upload_bytes`，避免白传一遍再被拒） */
  size: number;
}

/** 从相册选一张商品图。取消返回 null；无权限抛错（由页面翻成 Toast 文案）。 */
export async function pickImageFile(): Promise<PickedImage | null> {
  const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
  if (!permission.granted) throw new Error("需要相册权限才能上传商品图");
  const result = await ImagePicker.launchImageLibraryAsync({
    mediaTypes: ["images"],
    quality: 1,
    allowsMultipleSelection: false,
  });
  if (result.canceled || result.assets.length === 0) return null;
  const asset = result.assets[0];
  const type = asset.mimeType ?? "image/jpeg";
  const name = asset.fileName ?? `product-${Date.now()}.${type.split("/")[1] ?? "jpg"}`;
  return { file: { uri: asset.uri, name, type }, size: asset.fileSize ?? 0 };
}
