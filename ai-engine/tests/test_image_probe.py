"""图片字节探测单测（纯内存；无需 PG/Redis/真实 LLM/OSS）。

覆盖:
  · probe_image_size：PNG / JPEG / GIF / WEBP 四种格式读取**真实宽高**；
  · 失败不抛异常：空字节 / 非图片字节 / 截断字节一律返回 (None, None)；
  · probe_content_type：按魔数返回 MIME（决定 OSS PUT 的 Content-Type 与对象键后缀）。

为什么要有这组用例:
    历史缺陷是 image block 的 width/height 直接写「请求尺寸」（1024x1024），而网关实际出图
    尺寸可能不同（glm-image 默认 1280x1280）→ 元数据撒谎、前端按假尺寸渲染即尺寸不一致。
    尺寸事实只能来自字节本身，故探测器的正确性是元数据可信度的底线。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.workflowcore.node.image_probe import probe_content_type, probe_image_size


# 各格式的探测期望：(Pillow 保存格式, 期望 MIME)
FORMATS = [
    ("PNG", "image/png"),
    ("JPEG", "image/jpeg"),
    ("GIF", "image/gif"),
    ("WEBP", "image/webp"),
    ("BMP", "image/bmp"),
]


def _encode(fmt: str, size: tuple[int, int] = (640, 360)) -> bytes:
    """用 Pillow 生成指定格式/尺寸的图片字节（作为探测输入）。

    参数:
        fmt: Pillow 保存格式（PNG / JPEG / GIF / WEBP / BMP）。
        size: 图片宽高。
    返回:
        编码后的图片字节。
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize("fmt,mime", FORMATS)
def test_probe_image_size_reads_true_dimensions(fmt: str, mime: str) -> None:
    """四种受支持格式都必须读出字节里的真实宽高（而不是传入的其它任何值）。"""
    assert probe_image_size(_encode(fmt, (640, 360))) == (640, 360)
    assert probe_image_size(_encode(fmt, (99, 1280))) == (99, 1280)


@pytest.mark.parametrize("fmt,mime", FORMATS)
def test_probe_content_type_matches_real_format(fmt: str, mime: str) -> None:
    """MIME 必须与字节真实格式一致（OSS 预签名把 Content-Type 算进签名串）。"""
    assert probe_content_type(_encode(fmt)) == mime


@pytest.mark.parametrize(
    "broken",
    [
        b"",
        b"not-an-image-at-all",
        b"\x89PNG\r\n\x1a\n",  # 只有 PNG 签名，没有 IHDR
        b"\xff\xd8",  # 只有 JPEG SOI，没有段
        b"GIF89a",  # 只有 GIF 签名，没有逻辑屏幕描述符
        b"RIFF\x00\x00\x00\x00WEBP",  # 只有 WEBP 容器头，没有块
    ],
)
def test_probe_never_raises_and_returns_none_on_broken_bytes(broken: bytes) -> None:
    """畸形/截断字节只应降级为「无元数据」，绝不抛异常打断配图链路。"""
    assert probe_image_size(broken) == (None, None)
    assert probe_content_type(broken) in (None, "image/png", "image/jpeg", "image/gif", "image/webp")


def test_probe_content_type_unknown_returns_none() -> None:
    """无法识别的格式返回 None，由调用方兜底（node_image 用 image/png 与 .jpg 之外的默认）。"""
    assert probe_content_type(b"PK\x03\x04fake-zip") is None
    assert probe_content_type(b"") is None


def test_probe_image_size_truncated_jpeg_body_returns_none() -> None:
    """JPEG 关键段（SOF）缺失时必须返回 None，不能从压缩数据里瞎猜尺寸。"""
    data = _encode("JPEG")
    assert probe_image_size(data[:20]) == (None, None)
