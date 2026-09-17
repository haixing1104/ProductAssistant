"""image_probe：纯标准库读取图片字节的真实宽高（PNG / JPEG / GIF / WEBP）。

用途:
    image block 的 width/height 必须反映**图片字节的事实**，而不是「请求参数」。
    历史缺陷：直接把请求尺寸（如 1024x1024）写进 block，而网关/模型实际出图尺寸可能不同
    （如 glm-image 默认 1280x1280）→ 元数据与图片不一致，前端按假尺寸渲染即「尺寸不一致」。
    未注入 ImageNormalizer 时，本模块是尺寸事实的唯一来源。

支持:
    · PNG：IHDR 块；
    · JPEG：SOF0/1/2/3/5/6/7/9/10/11/13/14/15 段；
    · GIF：逻辑屏幕描述符；
    · WEBP：VP8X（容器）/ VP8 （有损）/ VP8L（无损）；
    · BMP：BITMAPINFOHEADER（宽/高为 int32 小端；高度为负表示自上而下存储）。
    其它格式返回 (None, None)，调用方按「无元数据」处理（不阻断链路）。

依赖:
    仅标准库（不引入 Pillow）：探测失败/不支持一律返回 (None, None)，绝不抛异常打断配图。
"""

from __future__ import annotations

# PNG 文件签名（8 字节）
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# JPEG SOFn 段（含帧头尺寸的标记）；不含 DHT/DAC 等无关段
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _sane(width: int, height: int) -> tuple[int | None, int | None]:
    """尺寸合法性收敛：非正数视为探测失败（防御畸形文件）。

    参数:
        width: 解析出的宽度。
        height: 解析出的高度。
    返回:
        (width, height)；任一非正数时返回 (None, None)。
    """
    if width <= 0 or height <= 0:
        return None, None
    return width, height


def _probe_png(data: bytes) -> tuple[int | None, int | None]:
    """读 PNG IHDR：签名(8) + 长度(4) + 类型(4)=\"IHDR\" + 宽(4,BE) + 高(4,BE)。

    参数:
        data: 文件字节。
    返回:
        (width, height)；结构不符返回 (None, None)。
    """
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None, None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return _sane(width, height)


def _probe_jpeg(data: bytes) -> tuple[int | None, int | None]:
    """遍历 JPEG 段链，命中 SOFn 段即读出尺寸（段内偏移：精度1 + 高2 + 宽2）。

    参数:
        data: 文件字节。
    返回:
        (width, height)；未找到 SOFn（如单段扫描数据截断）返回 (None, None)。
    注意:
        SOS(0xDA) 之后的熵编码数据不再按段解析，直接终止循环以避免误读压缩数据。
    """
    pos = 2  # 跳过 SOI(0xFFD8)
    total = len(data)
    while pos + 3 < total:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # 段间填充字节
            pos += 1
            continue
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2  # 无长度字段的标记
            continue
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS：尺寸信息必然已过
            return None, None
        if pos + 4 > total:
            return None, None
        seg_len = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if seg_len < 2:
            return None, None
        if marker in _JPEG_SOF_MARKERS:
            if pos + 9 > total:
                return None, None
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return _sane(width, height)
        pos += 2 + seg_len
    return None, None


def _probe_gif(data: bytes) -> tuple[int | None, int | None]:
    """读 GIF 逻辑屏幕描述符：签名(6) + 宽(2,LE) + 高(2,LE)。

    参数:
        data: 文件字节。
    返回:
        (width, height)；结构不符返回 (None, None)。
    """
    if len(data) < 10:
        return None, None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return _sane(width, height)


def _probe_webp(data: bytes) -> tuple[int | None, int | None]:
    """读 WEBP 三种块头之一（VP8X 容器 / VP8 有损 / VP8L 无损）的尺寸。

    参数:
        data: 文件字节。
    返回:
        (width, height)；结构不符/未知块返回 (None, None)。
    注意:
        布局：RIFF(4) + 大小(4) + \"WEBP\"(4) + 块类型(4) + 块大小(4) + 载荷。
        VP8X 用 24 位小端存储「宽-1 / 高-1」；VP8 帧头的宽高各 14 位有效；
        VP8L 用 28 位小端打包（低 14 位宽-1、次 14 位高-1）。
    """
    if len(data) < 30:
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return _sane(width, height)
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return _sane(width, height)
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return _sane(width, height)
    return None, None


def _probe_bmp(data: bytes) -> tuple[int | None, int | None]:
    """读 BMP BITMAPINFOHEADER：偏移 18 宽、偏移 22 高（均为 int32 小端）。

    参数:
        data: 文件字节。
    返回:
        (width, height)；结构不符返回 (None, None)。
    注意:
        高度为负表示「自上而下」存储（top-down DIB），取绝对值即可。
    """
    if len(data) < 26 or data[:2] != b"BM":
        return None, None
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    return _sane(abs(width), abs(height))


def probe_content_type(data: bytes) -> str | None:
    """探测图片 MIME（按魔数），用于 OSS PUT 的 Content-Type。

    为什么必须有：OSS SigV1 预签名把 Content-Type 算进签名串（adapters/oss.py::_presign），
    且对象键后缀应与真实格式一致 —— 历史实现把任何生图产物都按 image/png 上传，
    一旦网关返回 JPEG 就会出现「扩展名/Content-Type 与字节不符」。

    参数:
        data: 图片文件字节。
    返回:
        MIME 字符串（image/png | image/jpeg | image/gif | image/webp | image/bmp）；
        无法识别时返回 None（调用方自行兜底）。
    """
    if not data:
        return None
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def probe_image_size(data: bytes) -> tuple[int | None, int | None]:
    """探测图片字节的真实宽高（按魔数分派到各格式解析器）。

    参数:
        data: 图片文件字节；空字节返回 (None, None)。
    返回:
        (width, height)；不支持的格式或解析失败返回 (None, None)。
    注意:
        本函数不抛异常：配图链路上的元数据缺失只应降级，不应阻断落库/HITL。
    """
    if not data:
        return None, None
    if data.startswith(_PNG_SIGNATURE):
        return _probe_png(data)
    if data.startswith(b"\xff\xd8"):
        return _probe_jpeg(data)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _probe_gif(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _probe_webp(data)
    if data[:2] == b"BM":
        return _probe_bmp(data)
    return None, None
