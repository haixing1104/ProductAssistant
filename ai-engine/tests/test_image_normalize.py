"""图片规格化单测（Pillow；纯内存，无需 PG/Redis/真实 LLM/OSS）。

覆盖:
  · 输出规格：任意输入 → 目标尺寸正方形 + 统一 MIME；
  · 补边语义：居中等比缩放，**不裁剪**、**默认不放大**（小图保留原始像素密度）；
    （布局断言用 fmt="png"：JPEG 有损编码会把 8x8 块边缘混成补边色，按像素计量会有 ±2px 抖动）
  · 大图下采样、EXIF 方向修正、alpha 合成到背景；
  · AI 标识：ai_marked=True 写 EXIF（履行标识义务），ai_marked=False（实拍图）绝不携带；
  · 参数与失败：构造期非法参数抛 ValueError，运行期空字节/坏字节/非法入参抛
    ImageNormalizationError（调用方按降级处理）；
  · 环境变量工厂：默认启用、可关闭、非法配置回退默认值（不让 worker 起不来）。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.adapters.image_pillow import ImageNormalizationError, PillowImageNormalizer
from src.adapters.image_pillow import build_image_normalizer_from_env
from src.ports import DEFAULT_SIZE

RED = (255, 0, 0)
WHITE = (255, 255, 255)


def _encode(
    size: tuple[int, int] = (500, 250),
    fmt: str = "JPEG",
    color: tuple = RED,
    mode: str = "RGB",
    **save_kwargs,
) -> bytes:
    """生成测试图片字节。

    参数:
        size: 图片宽高。
        fmt: Pillow 保存格式。
        color: 填充色（决定内容区域颜色，便于按像素断言布局）。
        mode: 图像模式（"RGB" / "RGBA"）。
        save_kwargs: 透传给 Image.save（如 exif）。
    返回:
        编码后的图片字节。
    """
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    """从字节打开图片（返回已加载的独立副本，避免依赖外部 BytesIO 生命周期）。

    参数:
        data: 图片字节。
    返回:
        PIL.Image（已 load）。
    """
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _line(image: Image.Image, axis: str) -> list[tuple]:
    """取过图心的一条扫描线像素。

    参数:
        image: 待扫描图片。
        axis: "x" 表示横扫一行（横向布局），"y" 表示纵扫一列（纵向布局）。
    返回:
        像素列表。
    """
    if axis == "x":
        y = image.height // 2
        return [image.getpixel((x, y)) for x in range(image.width)]
    x = image.width // 2
    return [image.getpixel((x, y)) for y in range(image.height)]


def _content_span(image: Image.Image, axis: str, color: tuple = RED, tol: int = 16):
    """返回扫描线上「内容色」的首末下标（用于断言补边宽度/内容尺寸）。

    参数:
        image: 待扫描图片。
        axis: "x" 横向 / "y" 纵向。
        color: 内容色。
        tol: 每通道容差（JPEG 有损编码会让 255 变成 254 之类）。
    返回:
        (first, last) 下标元组；无命中返回 None。
    """
    hits = [
        i
        for i, px in enumerate(_line(image, axis))
        if all(abs(int(a) - int(b)) <= tol for a, b in zip(px[:3], color))
    ]
    return (hits[0], hits[-1]) if hits else None


def _near(pixel: tuple, color: tuple, tol: int = 16) -> bool:
    """判断像素是否近似等于目标色（容 JPEG 有损误差）。

    参数:
        pixel: 实际像素（RGB 或 RGBA）。
        color: 目标色。
        tol: 每通道容差。
    返回:
        近似相等返回 True。
    """
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(pixel[:3], color))


@pytest.mark.parametrize("fmt,mime", [("jpeg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")])
def test_normalize_outputs_target_size_and_mime(fmt: str, mime: str) -> None:
    """任意输入都输出目标正方形与对应 MIME（尺寸元数据的事实来源）。"""
    result = PillowImageNormalizer().normalize(_encode((500, 250)), fmt=fmt)
    assert (result.width, result.height) == DEFAULT_SIZE
    assert result.content_type == mime
    assert _open(result.data).size == DEFAULT_SIZE


def test_normalize_center_pads_without_cropping() -> None:
    """500x250 内容必须**完整保留**并居中补白边（不得裁剪、不得拉伸变形）。"""
    image = _open(PillowImageNormalizer().normalize(_encode((500, 250)), fmt="png").data)
    x_span, y_span = _content_span(image, "x"), _content_span(image, "y")
    assert x_span == (250, 749), "内容宽度应保持 500px 且左右各补 250px"
    assert y_span == (375, 624), "内容高度应保持 250px 且上下各补 375px"
    assert _near(image.getpixel((0, 0)), WHITE), "四角应为补边背景色"


def test_normalize_does_not_upscale_by_default() -> None:
    """默认不放大：小图保持原始像素密度，只补白边（避免插值糊图）。"""
    image = _open(PillowImageNormalizer().normalize(_encode((500, 500)), fmt="png").data)
    first, last = _content_span(image, "x")
    assert last - first + 1 == 500, "500px 输入不应被放大到 1000px"
    assert (first, last) == (250, 749)


def test_normalize_upscales_only_when_allowed() -> None:
    """显式允许放大时才插值填满（业务侧要「大图」时使用）。"""
    image = _open(PillowImageNormalizer(allow_upscale=True).normalize(_encode((500, 500))).data)
    assert _content_span(image, "x") == (0, 999)
    assert _content_span(image, "y") == (0, 999)


def test_normalize_downscales_oversized_input() -> None:
    """超出目标的大图按最长边等比缩到框内，再补边。"""
    image = _open(PillowImageNormalizer().normalize(_encode((2000, 1000))).data)
    first, last = _content_span(image, "x")
    assert last - first + 1 == 1000, "2000px 宽应缩到 1000px"
    y_first, y_last = _content_span(image, "y")
    assert 480 <= y_last - y_first + 1 <= 520, "高度应等比缩到约 500px（JPEG 边缘抖动允许容差）"


def test_normalize_applies_exif_orientation() -> None:
    """EXIF Orientation=6 的横拍图必须被转正（否则上传图方向错乱）。"""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: 需要旋转 90° 显示
    data = _encode((400, 200), exif=exif)
    image = _open(PillowImageNormalizer().normalize(data, fmt="png").data)
    x_span, y_span = _content_span(image, "x"), _content_span(image, "y")
    assert x_span[1] - x_span[0] + 1 == 200, "转正后内容宽应为 200"
    assert y_span[1] - y_span[0] + 1 == 400, "转正后内容高应为 400"


def test_normalize_flattens_alpha_onto_background() -> None:
    """全透明 RGBA 应被合成到背景色（避免补边出现透明块/黑块）。"""
    image = _open(PillowImageNormalizer().normalize(_encode((300, 300), fmt="PNG", mode="RGBA", color=(255, 0, 0, 0))).data)
    assert image.mode == "RGB"
    assert _near(image.getpixel((500, 500)), WHITE)


def test_normalize_marks_ai_generated_metadata() -> None:
    """ai_marked=True 写入 AI 生成标识（履行《标识办法》用户侧标识义务）。"""
    data = PillowImageNormalizer().normalize(_encode(), ai_marked=True).data
    exif = _open(data).getexif()
    assert exif.get(0x0131) == "ProductAssistant (AI-generated)"
    assert "trainedAlgorithmicMedia" in str(exif.get(0x010E))


def test_normalize_does_not_mark_real_photos() -> None:
    """ai_marked=False（运营实拍图）绝不携带 AI 标识 —— 不得冒挂。"""
    data = PillowImageNormalizer().normalize(_encode()).data
    assert _open(data).getexif().get(0x0131) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": (0, 1000)},
        {"size": (1000, -1)},
        {"fmt": "tiff"},
        {"quality": 0},
        {"quality": 101},
    ],
)
def test_constructor_rejects_invalid_configuration(kwargs: dict) -> None:
    """配置错误必须在装配期暴露（而不是等第一条任务才在运行期炸）。"""
    with pytest.raises(ValueError):
        PillowImageNormalizer(**kwargs)


@pytest.mark.parametrize(
    "call",
    [
        lambda n: n.normalize(b""),
        lambda n: n.normalize(b"not-an-image"),
        lambda n: n.normalize(_encode(), size=(0, 0)),
        lambda n: n.normalize(_encode(), background="not-a-color"),
        lambda n: n.normalize(_encode(), fmt="tiff"),
    ],
)
def test_normalize_raises_domain_error(call) -> None:
    """运行期失败抛 ImageNormalizationError（node_image 据此降级/回退，绝不落空图）。"""
    with pytest.raises(ImageNormalizationError):
        call(PillowImageNormalizer())


def test_factory_enabled_by_default(monkeypatch) -> None:
    """默认启用规格化（本项目的默认口径：统一 1000x1000 / jpeg）。"""
    monkeypatch.delenv("IMAGE_NORMALIZE_ENABLED", raising=False)
    normalizer = build_image_normalizer_from_env()
    assert isinstance(normalizer, PillowImageNormalizer)
    assert normalizer.size == DEFAULT_SIZE and normalizer.fmt == "jpeg"


def test_factory_can_be_disabled(monkeypatch) -> None:
    """显式关闭时返回 None（node_image 回退「不规格化」路径，行为同接入前）。"""
    monkeypatch.setenv("IMAGE_NORMALIZE_ENABLED", "0")
    assert build_image_normalizer_from_env() is None


def test_factory_falls_back_on_invalid_env(monkeypatch) -> None:
    """非法配置回退默认值而不是抛异常：配图是可选增强，不该让 worker 起不来。"""
    monkeypatch.setenv("IMAGE_NORMALIZE_SIZE", "garbage")
    monkeypatch.setenv("IMAGE_NORMALIZE_FORMAT", "tiff")
    monkeypatch.setenv("IMAGE_NORMALIZE_QUALITY", "999")
    normalizer = build_image_normalizer_from_env()
    assert normalizer.size == DEFAULT_SIZE
    assert normalizer.fmt == "jpeg"
    assert normalizer.quality == 100


def test_factory_reads_size_format_and_upscale(monkeypatch) -> None:
    """合法配置项必须生效（含 not-upscale 的显式开启）。"""
    monkeypatch.setenv("IMAGE_NORMALIZE_SIZE", "800x800")
    monkeypatch.setenv("IMAGE_NORMALIZE_FORMAT", "WEBP")
    monkeypatch.setenv("IMAGE_NORMALIZE_QUALITY", "70")
    monkeypatch.setenv("IMAGE_NORMALIZE_UPSCALE", "yes")
    monkeypatch.setenv("IMAGE_NORMALIZE_BACKGROUND", "#000000")
    normalizer = build_image_normalizer_from_env()
    assert (normalizer.size, normalizer.fmt, normalizer.quality) == ((800, 800), "webp", 70)
    assert normalizer.allow_upscale is True and normalizer.background == "#000000"
    result = normalizer.normalize(_encode((300, 300)))
    assert (result.width, result.height) == (800, 800)
