"""ImageNormalizer 端口：图片字节规格化的边界（统一尺寸 / 格式 / 背景）。

职责:
    把「任意来源的图片字节」（AI 生图产物 / 运营上传图）规格化为统一的电商商品图规格：
    EXIF 方向修正 → 等比缩放 → 补边到目标正方形（contain + pad）→ 统一编码格式与质量，
    并返回**实测输出尺寸**，供 image block 的 width/height 使用
    （元数据以字节事实为准，而不是「请求参数」）。

约束:
    · 本端口是纯函数式字节变换（无 DB/Redis/HTTP IO），实现方**不得**内部发起网络请求；
      输入字节由调用方准备（AI 产物直接来自 LLMImageGenGateway；上传图由
      ObjectStorageServer.get_bytes 取回）；
    · 未注入实现时 node_image 回退「不规格化」路径（仅用 image_probe 读实测尺寸），
      链路行为与未接入本端口前完全一致（与 rule_engine=None / rag_store=None 同哲学）。

实现见 adapters/image_pillow.py（Pillow）；工厂 build_image_normalizer_from_env()。

说明:
    · 输出尺寸与输入宽高比无关（保证 1:1 规格统一）；
    · 默认不放大（allow_upscale=False）：小图补背景边而不是插值放大，避免糊图；
    · ai_marked 仅用于 AI 生图产物：写入 AI 生成标识元数据
      （履行《人工智能生成合成内容标识办法》用户侧标识义务）；
      运营实拍图必须为 False，绝不冒挂 AI 标识。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

# 输出格式：jpeg（默认，体积小）/ png（需透明或无损）/ webp（更省流量）
ImageFormat = Literal["jpeg", "png", "webp"]

# 格式 → MIME：喂给 ObjectStorageServer.put_bytes 的 content_type。
# 注意 OSS SigV1 预签名把 Content-Type 算进签名串（见 adapters/oss.py::_presign），
# 故这里必须与实际上传头完全一致，否则签名不匹配被拒。
CONTENT_TYPES: dict[str, str] = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

# 格式 → 文件扩展名：对象键后缀必须与 Content-Type 一致（否则 CDN/浏览器可能误判）。
FILE_EXTENSIONS: dict[str, str] = {"jpeg": "jpg", "png": "png", "webp": "webp"}

# 默认目标规格：1:1、1000x1000（三平台主图/封面通用量级）
DEFAULT_SIZE: tuple[int, int] = (1000, 1000)

# 默认补边背景：纯白（电商白底图惯例）
DEFAULT_BACKGROUND = "#FFFFFF"

# 默认输出质量（PNG 忽略该参数）
DEFAULT_QUALITY = 85


@dataclass(frozen=True)
class NormalizedImage:
    """规格化结果（**实测事实**，非请求参数）。

    注意:
        width/height 是编码后字节的真实尺寸，调用方应直接写入 image block，
        不要再回填「请求尺寸」（历史缺陷：写死请求值导致元数据与图片不一致）。
    """

    data: bytes
    width: int
    height: int
    content_type: str


def content_type_for(fmt: str) -> str:
    """输出格式 → MIME；未知格式按 jpeg 处理（宁可统一，也不抛异常打断配图）。

    参数:
        fmt: 输出格式（jpeg / png / webp）。
    返回:
        对应 MIME 字符串；未知值返回 image/jpeg。
    """
    return CONTENT_TYPES.get(fmt, CONTENT_TYPES["jpeg"])


def file_extension_for(fmt: str) -> str:
    """输出格式 → 文件扩展名；未知格式按 jpg 处理。

    参数:
        fmt: 输出格式（jpeg / png / webp）。
    返回:
        扩展名（不含点）；未知值返回 jpg。
    """
    return FILE_EXTENSIONS.get(fmt, FILE_EXTENSIONS["jpeg"])


# MIME → 文件扩展名（反查）：未走规格化器时按字节真实格式决定对象键后缀/Content-Type。
EXTENSIONS_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def file_extension_for_content_type(content_type: str) -> str:
    """MIME → 文件扩展名（未收录按 jpg）。

    参数:
        content_type: MIME 字符串（如 image/jpeg）。
    返回:
        扩展名（不含点）；未收录时返回 jpg。
    """
    return EXTENSIONS_BY_CONTENT_TYPE.get(content_type, "jpg")


class ImageNormalizer(ABC):
    """图片规格化端口（纯内存字节变换，无副作用）。"""

    @abstractmethod
    def normalize(
        self,
        data: bytes,
        *,
        size: tuple[int, int] | None = None,
        background: str | None = None,
        fmt: ImageFormat | None = None,
        quality: int | None = None,
        allow_upscale: bool | None = None,
        ai_marked: bool = False,
    ) -> NormalizedImage:
        """把图片字节规格化为统一规格，返回实测尺寸与 MIME。

        参数:
            data: 原始图片字节（受支持格式：JPEG/PNG/WEBP/BMP/GIF/TIFF 等）。
            size: 目标尺寸 (width, height)；None 用实现方默认（1000x1000）。
            background: 补边背景色（如 "#FFFFFF"）；None 用实现方默认。
            fmt: 输出格式；None 用实现方默认（jpeg）。
            quality: 输出质量 1-100；None 用实现方默认（85）。
            allow_upscale: 是否允许插值放大；None 用实现方默认（False 不放大）。
            ai_marked: 是否写入 AI 生成标识元数据；**仅 AI 生图产物可为 True**，
                运营实拍图必须保持 False（不得冒挂 AI 标识）。
        返回:
            NormalizedImage：编码后字节 + 实测宽高 + MIME。
        异常:
            Exception: 解码 / 编码失败由实现方抛出（如 ImageNormalizationError）；
                调用方（node_image）按「单张失败降级/回退原图」处理，不阻断主链路。
        注意:
            实现方不得发起任何网络 IO；也不得因输入不可解码而返回空字节——
            失败必须抛异常，避免把空图当作成功产物落库。
        """
