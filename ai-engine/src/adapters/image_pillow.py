"""ImageNormalizer 的 Pillow 实现：字节级规格化（EXIF 方向 → 等比缩放 → 补边 → 统一编码）。

流程:
    1. Image.open(BytesIO) 解码；
    2. ImageOps.exif_transpose() 修正拍摄方向（运营上传图常见「手机竖拍横放」）；
    3. 等比缩放到目标框内（取「宽高都装得下」的比例）：**默认不放大**（小图保持原始像素
       密度，避免插值糊图），仅当 allow_upscale=True 时才放大；
    4. 居中补边到目标正方形（背景色填充）：**不裁剪**，商品本体完整保留
       （对比 cover/裁剪会切掉商品边缘，不适合商品图规格化）；
    5. 编码为 jpeg/png/webp，返回**实测输出尺寸**与 MIME。

环境变量:
    - IMAGE_NORMALIZE_ENABLED：1/true/yes/on 启用（**默认启用**）；0/false/no 时工厂返回 None
      （回退「不规格化」路径，链路行为与接入前一致）。
    - IMAGE_NORMALIZE_SIZE：目标尺寸 "1000x1000"（默认）。
    - IMAGE_NORMALIZE_FORMAT：jpeg|png|webp（默认 jpeg；需透明/无损时用 png）。
    - IMAGE_NORMALIZE_QUALITY：1-100（默认 85；png 忽略）。
    - IMAGE_NORMALIZE_BACKGROUND：补边背景色（默认 #FFFFFF）。
    - IMAGE_NORMALIZE_UPSCALE：1/true/yes/on 允许放大（默认关闭）。

依赖:
    仅 Pillow（见 requirements.txt）。未安装 Pillow 时工厂返回 None（不报错），
    node_image 回退「不规格化」路径。

合规:
    ai_marked=True 时写入 AI 生成标识元数据（EXIF ImageDescription / Software），
    用于履行《人工智能生成合成内容标识办法》用户侧标识义务；**只对 AI 生图产物生效**，
    运营实拍图必须传 False。
    注意：Pillow 重新编码会丢弃原元数据 —— 若把 ZHIPU_IMAGE_WATERMARK 打开
    （平台写入隐式数字水印），则**不应**再走本适配器重编码，否则可能触及
    「不得隐匿标识」条款（见 README「AI 配图与合规」）。

安全:
    保留 Pillow 默认的 Image.MAX_IMAGE_PIXELS 解压炸弹上限；超限直接抛异常，
    由 node_image 的降级分支兜住（不把整机内存吃光）。
"""

from __future__ import annotations

import io
import os

from ..ports.image_normalizer import (
    CONTENT_TYPES,
    DEFAULT_BACKGROUND,
    DEFAULT_QUALITY,
    DEFAULT_SIZE,
    ImageNormalizer,
    NormalizedImage,
    content_type_for,
)

try:  # pragma: no cover - 未安装 Pillow 的部署走下方 None 分支
    from PIL import Image, ImageColor, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageColor = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

# AI 生成标识（写入 EXIF；仅 ai_marked=True 的 AI 生图产物携带）
_AI_MARK_SOFTWARE = "ProductAssistant (AI-generated)"
_AI_MARK_DESCRIPTION = "AI-generated content; DigitalSourceType=trainedAlgorithmicMedia"


def _truthy(raw: str | None, *, default: bool) -> bool:
    """把环境变量收敛为布尔。

    参数:
        raw: 环境变量原始值（None 表示未配置）。
        default: 未配置或空白时的取值。
    返回:
        1/true/yes/on 为 True，其余非空值为 False，未配置为 default。
    """
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_size(raw: str | None) -> tuple[int, int]:
    """解析 "1000x1000" → (1000, 1000)；非法/缺失时回退默认尺寸（不打断链路）。

    参数:
        raw: 形如 "1000x1000" 的环境变量值。
    返回:
        (width, height)；解析失败返回 DEFAULT_SIZE。
    """
    if not raw:
        return DEFAULT_SIZE
    try:
        width_raw, height_raw = raw.strip().lower().split("x")
        width, height = int(width_raw), int(height_raw)
    except (ValueError, AttributeError):
        return DEFAULT_SIZE
    if width <= 0 or height <= 0:
        return DEFAULT_SIZE
    return width, height


def _build_ai_exif() -> bytes | None:
    """构造 AI 生成标识 EXIF 字节（ImageDescription + Software）。

    返回:
        EXIF 字节；Pillow 不可用或构造失败时返回 None（标识尽力而为，绝不阻断配图）。
    """
    if Image is None:
        return None
    try:
        exif = Image.Exif()
        exif[0x010E] = _AI_MARK_DESCRIPTION  # ImageDescription
        exif[0x0131] = _AI_MARK_SOFTWARE  # Software
        return exif.tobytes()
    except Exception:  # noqa: BLE001 元数据写入失败不应阻断配图主链路
        return None


class ImageNormalizationError(RuntimeError):
    """图片规格化异常（未安装 Pillow / 解码失败 / 参数非法 / 编码失败）。"""


class PillowImageNormalizer(ImageNormalizer):
    """ImageNormalizer 的 Pillow 实现（同步、纯内存、无网络 IO）。

    构造参数即默认规格；normalize() 的入参可逐项覆盖（None = 用本对象的默认值）。
    """

    def __init__(
        self,
        *,
        size: tuple[int, int] = DEFAULT_SIZE,
        background: str = DEFAULT_BACKGROUND,
        fmt: str = "jpeg",
        quality: int = DEFAULT_QUALITY,
        allow_upscale: bool = False,
    ) -> None:
        """初始化规格化器（仅校验参数，不做 IO / 不加载图片）。

        参数:
            size: 目标尺寸 (width, height)，均须为正整数。
            background: 补边背景色（十六进制串，如 "#FFFFFF"）。
            fmt: 输出格式（jpeg / png / webp）。
            quality: 输出质量 1-100（png 忽略）。
            allow_upscale: 是否允许插值放大（默认 False）。
        异常:
            ValueError: 尺寸非正、格式不支持或质量越界（配置错误应在装配期暴露）。
        """
        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            raise ValueError(f"目标尺寸必须为正整数: {size}")
        if fmt not in CONTENT_TYPES:
            raise ValueError(f"不支持的输出格式: {fmt}（可选 {sorted(CONTENT_TYPES)}）")
        quality_int = int(quality)
        if not 1 <= quality_int <= 100:
            raise ValueError(f"输出质量必须在 1-100: {quality}")
        self.size = (width, height)
        self.background = background or DEFAULT_BACKGROUND
        self.fmt = fmt
        self.quality = quality_int
        self.allow_upscale = bool(allow_upscale)

    @staticmethod
    def _save_kwargs(fmt: str, quality: int) -> dict:
        """按输出格式给出 Pillow save 参数。

        参数:
            fmt: 输出格式（jpeg / png / webp）。
            quality: 质量（png 忽略）。
        返回:
            save(**kwargs) 用的参数字典。
        """
        if fmt == "jpeg":
            return {"format": "JPEG", "quality": quality, "optimize": True, "progressive": True}
        if fmt == "webp":
            return {"format": "WEBP", "quality": quality, "method": 6}
        return {"format": "PNG", "optimize": True}

    @staticmethod
    def _flatten_to_rgb(image, background_rgb: tuple[int, int, int]):
        """把任意模式图像转成 RGB：带 alpha 的先合成到背景色，避免补边出现透明块。

        参数:
            image: PIL.Image 实例（任意模式）。
            background_rgb: 合成用背景色 RGB 三元组。
        返回:
            RGB 模式的 PIL.Image。
        """
        if image.mode == "RGB":
            return image
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, background_rgb)
            canvas.paste(rgba, mask=rgba.split()[-1])
            return canvas
        return image.convert("RGB")

    def normalize(
        self,
        data: bytes,
        *,
        size: tuple[int, int] | None = None,
        background: str | None = None,
        fmt: str | None = None,
        quality: int | None = None,
        allow_upscale: bool | None = None,
        ai_marked: bool = False,
    ) -> NormalizedImage:
        """把图片字节规格化为统一规格（EXIF 方向修正 → 等比缩放 → 补边 → 编码）。

        参数:
            data: 原始图片字节。
            size: 目标尺寸；None 用本对象 size。
            background: 补边背景色；None 用本对象 background。
            fmt: 输出格式；None 用本对象 fmt。
            quality: 输出质量；None 用本对象 quality。
            allow_upscale: 是否允许放大；None 用本对象 allow_upscale。
            ai_marked: 是否写入 AI 生成标识元数据（**仅 AI 生图产物传 True**）。
        返回:
            NormalizedImage：编码后字节 + **实测输出宽高** + MIME。
        异常:
            ImageNormalizationError: 未安装 Pillow、字节为空、解码/编码失败、参数非法。
        注意:
            输出尺寸恒等于目标尺寸（补边保证 1:1）；不放大模式下商品本体保持原始像素密度。
        """
        if Image is None or ImageOps is None or ImageColor is None:
            raise ImageNormalizationError("未安装 Pillow，无法执行图片规格化")
        if not data:
            raise ImageNormalizationError("空图片字节，无法规格化")
        target = self.size if size is None else (int(size[0]), int(size[1]))
        if target[0] <= 0 or target[1] <= 0:
            raise ImageNormalizationError(f"目标尺寸非法: {target}")
        bg_hex = background or self.background
        try:
            bg_rgb = tuple(ImageColor.getrgb(bg_hex)[:3])
        except ValueError as exc:
            raise ImageNormalizationError(f"背景色非法: {bg_hex}") from exc
        out_fmt = fmt or self.fmt
        if out_fmt not in CONTENT_TYPES:
            raise ImageNormalizationError(f"不支持的输出格式: {out_fmt}")
        out_quality = self.quality if quality is None else int(quality)
        upscale = self.allow_upscale if allow_upscale is None else bool(allow_upscale)

        try:
            with Image.open(io.BytesIO(data)) as src:
                src.load()
                image = ImageOps.exif_transpose(src)  # 方向修正（EXIF 旋转/镜像）
        except Exception as exc:  # noqa: BLE001 解码失败统一转为域异常（含解压炸弹）
            raise ImageNormalizationError(f"图片解码失败: {exc!r}") from exc

        # 等比缩放到目标框内：scale 取「宽高都装得下」的比例，默认再夹到 <=1（小图不放大，避免糊图）。
        # 注意：不能用 ImageOps.pad/contain —— 它们在补边前会把图放大去填满一条边（500x250 → 1000x500），
        # 与「不放大」语义冲突（实测踩过），故此处自己算 fit 尺寸并居中粘贴。
        scale = min(target[0] / image.width, target[1] / image.height)
        if not upscale:
            scale = min(1.0, scale)
        fit_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        if fit_size != (image.width, image.height):
            image = image.resize(fit_size, Image.Resampling.LANCZOS)

        rgb = self._flatten_to_rgb(image, bg_rgb)
        # 居中补边到目标正方形（背景色填充；不裁剪，商品本体完整）
        padded = Image.new("RGB", target, bg_rgb)
        padded.paste(rgb, ((target[0] - rgb.width) // 2, (target[1] - rgb.height) // 2))

        out = io.BytesIO()
        save_kwargs = self._save_kwargs(out_fmt, out_quality)
        exif_bytes = _build_ai_exif() if ai_marked else None
        if exif_bytes is None:
            padded.save(out, **save_kwargs)
        else:
            save_kwargs["exif"] = exif_bytes
            try:
                padded.save(out, **save_kwargs)
            except (TypeError, ValueError, OSError):
                # 某些格式/版本不接受 exif 参数：退化为不写元数据，绝不因此丢图
                out = io.BytesIO()
                save_kwargs.pop("exif", None)
                padded.save(out, **save_kwargs)
        return NormalizedImage(
            data=out.getvalue(),
            width=padded.width,
            height=padded.height,
            content_type=content_type_for(out_fmt),
        )


def build_image_normalizer_from_env() -> ImageNormalizer | None:
    """按环境变量构建规格化器；未启用或未安装 Pillow 时返回 None。

    返回 None 时 node_image 回退「不规格化」路径（仅探测实测尺寸），
    链路行为与未接入本端口前完全一致。

    参数:
        （无；全部读取环境变量，见模块 docstring）
    返回:
        PillowImageNormalizer 实例；未启用 / 未安装 Pillow 时返回 None。
    注意:
        非法的 IMAGE_NORMALIZE_SIZE/FORMAT/QUALITY 一律回退默认值而不抛异常：
        配图属可选增强，配置写错不应让 worker 起不来。
    """
    if Image is None or ImageOps is None or ImageColor is None:
        return None
    if not _truthy(os.getenv("IMAGE_NORMALIZE_ENABLED"), default=True):
        return None
    fmt = (os.getenv("IMAGE_NORMALIZE_FORMAT") or "jpeg").strip().lower()
    if fmt not in CONTENT_TYPES:
        fmt = "jpeg"
    try:
        quality = int(os.getenv("IMAGE_NORMALIZE_QUALITY") or DEFAULT_QUALITY)
    except ValueError:
        quality = DEFAULT_QUALITY
    quality = min(100, max(1, quality))
    return PillowImageNormalizer(
        size=_parse_size(os.getenv("IMAGE_NORMALIZE_SIZE")),
        background=os.getenv("IMAGE_NORMALIZE_BACKGROUND") or DEFAULT_BACKGROUND,
        fmt=fmt,
        quality=quality,
        allow_upscale=_truthy(os.getenv("IMAGE_NORMALIZE_UPSCALE"), default=False),
    )


__all__ = ["ImageNormalizationError", "PillowImageNormalizer", "build_image_normalizer_from_env"]
