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

/**
 * 归一化 MIME：部分 Android 相册提供方会给出 `image/jpg`（非标准写法）。
 *
 * 为什么必须在**上传前**归一：这个字符串参与 OSS SigV1 签名（见 `rnPlatform.putBinary` 注释），
 * 必须"送 presign 的"与"PUT 带上的"逐字一致；同时它还要过上面的白名单。
 */
function normalizeImageType(type: string): string {
  return type === "image/jpg" ? "image/jpeg" : type;
}

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

/** 选中的图片：`file` 直接喂给共享层的预签名直传，`size` 用于上传前判上限。 */
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
  const type = normalizeImageType(asset.mimeType ?? "image/jpeg");
  // 白名单必须在**选完就判**：后端 presign 只接受 jpeg/png/webp（`ALLOWED_CONTENT_TYPES`），
  // 不拦的话 HEIC/GIF 会一路走到 presign 才失败，用户只看到一句"请求有误"（2026-09 真机反馈）。
  if (!IMAGE_TYPES.has(type)) {
    throw new Error(`只支持 JPG/PNG/WebP 图片（当前：${type}），请换一张或先转成 JPG`);
  }
  const name = asset.fileName ?? `product-${Date.now()}.${type.split("/")[1] ?? "jpg"}`;
  return { file: { uri: asset.uri, name, type }, size: asset.fileSize ?? 0 };
}
