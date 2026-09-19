// 选图 / 选文件（RN 专属能力）用例 —— 钉住"上传前必须拦下的两类输入"（2026-09 真机上传失败复盘）。
//
// ① **非白名单 MIME**（HEIC / GIF / WebP 之外的）：后端 presign 只接受 jpeg/png/webp
//    （`services/oss.ALLOWED_CONTENT_TYPES`）；不在这里拦，用户会在预签名阶段拿到一句含糊的错，
//    而根因是"这张图的格式根本不被支持"；
// ② `image/jpg` 这种**非标准写法**（部分 Android 相册提供方会给）：它参与 OSS SigV1 签名，
//    必须归一化，否则"送 presign 的"与"PUT 带上的"不一致 → 403 SignatureDoesNotMatch。
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";

import { IMAGE_TYPES, pickCsvFile, pickImageFile } from "../platform/pickFile";

jest.mock("expo-image-picker", () => ({
  requestMediaLibraryPermissionsAsync: jest.fn(),
  launchImageLibraryAsync: jest.fn(),
}));
jest.mock("expo-document-picker", () => ({ getDocumentAsync: jest.fn() }));

const picker = ImagePicker as unknown as {
  requestMediaLibraryPermissionsAsync: jest.Mock;
  launchImageLibraryAsync: jest.Mock;
};
/** CSV 选择器（导入商品用；后端按内容判 CSV，所以这里只做形状翻译）。 */
const docs = DocumentPicker as unknown as { getDocumentAsync: jest.Mock };

/** 造一次"用户选了这张图"的结果（expo-image-picker 的返回形状）。 */
function picked(mimeType: string | undefined, overrides: Record<string, unknown> = {}) {
  return {
    canceled: false,
    assets: [{ uri: "file:///tmp/a", fileName: "a.jpg", fileSize: 1024, mimeType, ...overrides }],
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  picker.requestMediaLibraryPermissionsAsync.mockResolvedValue({ granted: true });
});

describe("pickImageFile（商品图：格式必须在选完就判）", () => {
  it("白名单与后端 presign 同口径（jpeg / png / webp 三种）", () => {
    expect([...IMAGE_TYPES].sort()).toEqual(["image/jpeg", "image/png", "image/webp"]);
  });

  it("没有相册权限：抛出可读原因（页面翻成 Toast，而不是静默什么都不发生）", async () => {
    picker.requestMediaLibraryPermissionsAsync.mockResolvedValue({ granted: false });
    await expect(pickImageFile()).rejects.toThrow(/相册权限/);
    expect(picker.launchImageLibraryAsync).not.toHaveBeenCalled();
  });

  it("用户取消：返回 null（不算失败，也不弹提示）", async () => {
    picker.launchImageLibraryAsync.mockResolvedValue({ canceled: true, assets: [] });
    await expect(pickImageFile()).resolves.toBeNull();
  });

  it("HEIC/GIF 这类后端不接受的格式：**当场**给出可读原因（不是到 presign 才失败）", async () => {
    picker.launchImageLibraryAsync.mockResolvedValue(picked("image/heic"));
    await expect(pickImageFile()).rejects.toThrow(/只支持 JPG\/PNG\/WebP/);
  });

  it("`image/jpg` 归一化成 `image/jpeg`（非标准写法会让 OSS 签名对不上）", async () => {
    picker.launchImageLibraryAsync.mockResolvedValue(picked("image/jpg"));
    const result = await pickImageFile();
    expect(result?.file.type).toBe("image/jpeg");
    expect(result?.size).toBe(1024);
  });

  it("缺 mimeType 时按 jpeg 兜底（Android 部分提供方不给），并保留本地 uri（PUT 要它）", async () => {
    picker.launchImageLibraryAsync.mockResolvedValue(picked(undefined));
    const result = await pickImageFile();
    expect(result?.file).toMatchObject({ uri: "file:///tmp/a", type: "image/jpeg" });
  });
});

describe("pickCsvFile（商品批量导入）", () => {
  it("取消：返回 null", async () => {
    docs.getDocumentAsync.mockResolvedValue({ canceled: true, assets: [] });
    await expect(pickCsvFile()).resolves.toBeNull();
  });

  it("选中：翻译成共享层认的 `{uri,name,type}`（部分提供方不给 mimeType 时兜底 text/csv）", async () => {
    docs.getDocumentAsync.mockResolvedValue({
      canceled: false,
      assets: [{ uri: "file:///tmp/p.csv", name: "商品.csv", mimeType: "text/csv" }],
    });
    await expect(pickCsvFile()).resolves.toEqual({
      uri: "file:///tmp/p.csv",
      name: "商品.csv",
      type: "text/csv",
    });

    docs.getDocumentAsync.mockResolvedValue({
      canceled: false,
      assets: [{ uri: "file:///tmp/p.csv", name: "", mimeType: undefined }],
    });
    await expect(pickCsvFile()).resolves.toEqual({
      uri: "file:///tmp/p.csv",
      name: "products.csv",
      type: "text/csv",
    });
  });
});
