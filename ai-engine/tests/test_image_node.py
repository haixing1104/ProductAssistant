"""image 节点单测（假端口；不需要 PG/Redis/真实 LLM/OSS）。

覆盖（本轮修复的两条缺陷 + 回归红线）:
  · 尺寸元数据以**字节事实**为准：注入规格化器 → 写规格化后的实测尺寸；未注入 → 写原始
    字节的探测尺寸；**绝不写请求尺寸**（历史缺陷：写死 1024x1024，与真实出图尺寸不符）；
  · Content-Type 与对象键后缀跟随字节真实格式（历史：恒传 image/png；网关返回 JPEG 时不符）；
  · 上传图：取回字节 → 规格化（ai_marked=False，绝不冒挂 AI 标识）→ 转存**新** key；
    取不回（第三方外链/失败）→ 回退原 URL 且不写宽高（没有字节就没有事实尺寸）；
  · AI 图：规格化时 ai_marked=True（写入 AI 生成标识元数据，履行标识义务）；
  · 失败一律降级：生图/规格化/上传失败 → 纯文本 + image_error，绝不抛异常阻断落库；
  · 回归红线：未注入规格化器时行为与接入前一致（直接用原 URL / 只探测尺寸）。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.ports import (
    ImageNormalizer,
    LLMImageGenGateway,
    NormalizedImage,
    ObjectStorageServer,
)
from src.workflowcore.node.node_image import gen_image_node
from src.workflowcore.state import ListingState

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
THREAD_ID = "55555555-5555-4555-8555-555555555501"


def _encode(fmt: str = "JPEG", size: tuple[int, int] = (500, 250)) -> bytes:
    """生成测试图片字节。

    参数:
        fmt: Pillow 保存格式。
        size: 图片宽高。
    返回:
        编码后的图片字节。
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 0, 0)).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeImageGateway(LLMImageGenGateway):
    """假生图网关：返回预设字节（或抛预设异常），并记录调用参数。"""

    def __init__(self, payload: bytes = b"", error: Exception | None = None) -> None:
        """初始化。

        参数:
            payload: generate_image 的返回字节。
            error: 非 None 时 generate_image 抛出该异常。
        """
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        """返回预设字节（或抛预设异常）。

        参数:
            prompt: 生图提示词（被记录，供断言提示词包含商品事实）。
            size: 请求尺寸（被记录）。
        返回:
            预设图片字节。
        异常:
            Exception: self.error 非 None 时抛出。
        """
        self.calls.append((prompt, size))
        if self.error is not None:
            raise self.error
        return self.payload


class FakeStorage(ObjectStorageServer):
    """内存对象存储：记录上传，并按 URL 表模拟「本存储受管对象」的取回。"""

    def __init__(self, objects: dict | None = None, get_error: Exception | None = None) -> None:
        """初始化。

        参数:
            objects: url -> 字节 的受管对象表；未命中返回 None（模拟第三方外链）。
            get_error: 非 None 时 get_bytes 抛该异常（模拟网络故障）。
        """
        self.objects = dict(objects or {})
        self.get_error = get_error
        self.puts: list[tuple[str, bytes, str]] = []
        self.put_error: Exception | None = None

    def put_bytes(self, key: str, data: bytes, content_type: str = "image/png") -> str:
        """记录上传并返回伪 URL。

        参数:
            key: 对象键（被记录，供断言后缀与路径）。
            data: 图片字节（被记录）。
            content_type: MIME（被记录，供断言与字节一致）。
        返回:
            f"https://oss.test/{key}"。
        异常:
            Exception: self.put_error 非 None 时抛出。
        """
        self.puts.append((key, data, content_type))
        if self.put_error is not None:
            raise self.put_error
        return f"https://oss.test/{key}"

    def get_bytes(self, url: str) -> bytes | None:
        """按 URL 取字节：get_error 优先，其次查 objects，未命中返回 None。

        参数:
            url: 对象 URL。
        返回:
            对象字节或 None。
        异常:
            Exception: self.get_error 非 None 时抛出。
        """
        if self.get_error is not None:
            raise self.get_error
        return self.objects.get(url)

    def delete_key(self, key: str) -> bool:
        """假删除：恒 True（本用例不校验删除）。

        参数:
            key: 对象键。
        返回:
            True。
        """
        return True


class RecordingNormalizer(ImageNormalizer):
    """记录 ai_marked 的假规格化器：返回固定规格，便于断言调用契约。"""

    def __init__(
        self,
        *,
        width: int = 1000,
        height: int = 1000,
        content_type: str = "image/jpeg",
        error: Exception | None = None,
    ) -> None:
        """初始化。

        参数:
            width: 返回的实测宽。
            height: 返回的实测高。
            content_type: 返回的 MIME。
            error: 非 None 时 normalize 抛该异常。
        """
        self.width = width
        self.height = height
        self.content_type = content_type
        self.error = error
        self.marks: list[bool] = []

    def normalize(
        self,
        data: bytes,
        *,
        size=None,
        background=None,
        fmt=None,
        quality=None,
        allow_upscale=None,
        ai_marked: bool = False,
    ) -> NormalizedImage:
        """记录 ai_marked 并返回固定规格结果（或抛预设异常）。

        参数:
            data: 输入字节（原样作为输出字节，便于断言透传）。
            size: 目标尺寸（本假实现忽略）。
            background: 背景色（本假实现忽略）。
            fmt: 输出格式（本假实现忽略）。
            quality: 质量（本假实现忽略）。
            allow_upscale: 是否放大（本假实现忽略）。
            ai_marked: 是否写 AI 标识（被记录，核心断言点）。
        返回:
            NormalizedImage（固定 width/height/content_type）。
        异常:
            Exception: self.error 非 None 时抛出。
        """
        self.marks.append(ai_marked)
        if self.error is not None:
            raise self.error
        return NormalizedImage(
            data=data, width=self.width, height=self.height, content_type=self.content_type
        )


class FakeEventBus:
    """记录事件的最小事件总线（节点只调用 publish，故无需继承 EventBus）。"""

    def __init__(self) -> None:
        """初始化空事件列表。"""
        self.events: list[tuple[str, dict]] = []

    def publish(self, stream: str, payload: dict) -> None:
        """记录一次发布。

        参数:
            stream: 事件流键（如 evt:{thread_id}）。
            payload: 事件载荷。
        返回:
            无返回值。
        """
        self.events.append((stream, payload))


def _state(info: dict | None = None) -> ListingState:
    """构造节点入参状态（固定租户/线程，便于断言对象键）。

    参数:
        info: raw_product_info（默认空 → 走 AI 生图分支）。
    返回:
        ListingState 实例。
    """
    return ListingState(
        thread_id=THREAD_ID,
        product_id=PRODUCT_ID,
        org_id=ORG_ID,
        raw_product_info=info or {},
        generated_content="正文文案",
    )


def test_text_block_is_always_first() -> None:
    """content_blocks 的第一个 block 恒为 generated_content 的 text block。"""
    out = gen_image_node(_state({"title": "T", "raw_images": []}), image_gateway=FakeImageGateway(_encode()), object_storage=FakeStorage())
    assert out["content_blocks"][0] == {"type": "text", "text": "正文文案"}


def test_ai_branch_uses_normalized_facts_and_real_content_type() -> None:
    """注入规格化器时：宽高用规格化实测值、MIME 用规格化输出、后缀随之（JPEG→jpg）。"""
    gateway = FakeImageGateway(_encode("JPEG", (500, 250)))
    storage = FakeStorage()
    normalizer = RecordingNormalizer(width=1000, height=1000, content_type="image/jpeg")
    out = gen_image_node(_state(), image_gateway=gateway, object_storage=storage, image_normalizer=normalizer, image_key_prefix="img/pa/test")

    assert out["image_attached"] is True and out["image_error"] is None
    block = out["content_blocks"][1]
    assert (block["width"], block["height"]) == (1000, 1000)
    assert block["source"] == "ai_generated"
    assert block["url"].endswith("/cover-1.jpg"), "对象键后缀必须跟随真实编码格式"
    assert block["url"].endswith(f"img/pa/test/{ORG_ID}/{PRODUCT_ID}/{THREAD_ID}/cover-1.jpg")
    assert storage.puts[0][2] == "image/jpeg", "put_bytes 的 Content-Type 必须与字节一致（OSS 签名串含它）"
    assert normalizer.marks == [True], "AI 产物必须带 AI 标识（ai_marked=True）"


def test_ai_branch_without_normalizer_uses_true_size_not_requested_size() -> None:
    """回归红线：未注入规格化器时，宽高取字节实测值（500x250），绝不写请求尺寸 1024x1024。"""
    storage = FakeStorage()
    out = gen_image_node(
        _state(),
        image_gateway=FakeImageGateway(_encode("JPEG", (500, 250))),
        object_storage=storage,
        image_normalizer=None,
        image_size="1024x1024",
    )
    block = out["content_blocks"][1]
    assert (block["width"], block["height"]) == (500, 250)
    assert (block["width"], block["height"]) != (1024, 1024)
    assert block["url"].endswith("cover-1.jpg"), "后缀由字节探测决定（JPEG → jpg）"
    assert storage.puts[0][2] == "image/jpeg"


@pytest.mark.parametrize("fmt,ext,mime", [("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp")])
def test_ai_branch_extension_follows_real_format(fmt: str, ext: str, mime: str) -> None:
    """网关返回什么格式，对象键后缀与 Content-Type 就跟什么格式一致。"""
    storage = FakeStorage()
    out = gen_image_node(_state(), image_gateway=FakeImageGateway(_encode(fmt)), object_storage=storage)
    assert out["content_blocks"][1]["url"].endswith(f"cover-1.{ext}")
    assert storage.puts[0][2] == mime


def test_ai_branch_probes_unknown_bytes_without_crashing() -> None:
    """网关返回不可识别字节时：仍然落库（兜底 image/png），且不写宽高（不编造事实）。"""
    storage = FakeStorage()
    out = gen_image_node(_state(), image_gateway=FakeImageGateway(b"not-an-image"), object_storage=storage)
    block = out["content_blocks"][1]
    assert out["image_attached"] is True
    assert "width" not in block and "height" not in block
    assert storage.puts[0][2] == "image/png"


def test_ai_branch_gateway_failure_degrades_to_text() -> None:
    """生图异常必须降级纯文本并记录原因，绝不抛异常阻断落库/HITL。"""
    out = gen_image_node(_state(), image_gateway=FakeImageGateway(error=RuntimeError("网关 500")), object_storage=FakeStorage())
    assert out["image_attached"] is False
    assert len(out["content_blocks"]) == 1
    assert "AI 配图失败" in out["image_error"] and "网关 500" in out["image_error"]


def test_ai_branch_normalizer_failure_degrades_to_text() -> None:
    """规格化失败同样降级纯文本（不让一张图毁掉已通过文案）。"""
    normalizer = RecordingNormalizer(error=RuntimeError("解码失败"))
    out = gen_image_node(_state(), image_gateway=FakeImageGateway(_encode()), object_storage=FakeStorage(), image_normalizer=normalizer)
    assert out["image_attached"] is False and "AI 配图失败" in out["image_error"]


def test_ai_branch_upload_failure_degrades_to_text() -> None:
    """转存 OSS 失败同样降级纯文本。"""
    storage = FakeStorage()
    storage.put_error = RuntimeError("OSS 403")
    out = gen_image_node(_state(), image_gateway=FakeImageGateway(_encode()), object_storage=storage)
    assert out["image_attached"] is False and "OSS 403" in out["image_error"]


def test_ai_branch_without_ports_records_error() -> None:
    """缺生图网关或对象存储时：纯文本 + 明确原因（骨架/未配置部署的降级路径）。"""
    out = gen_image_node(_state(), image_gateway=None, object_storage=None)
    assert out["image_attached"] is False
    assert "未配置生图模型/对象存储" in out["image_error"]


def test_ai_branch_emits_events() -> None:
    """AI 分支应发 stage.imaging 与 image.ready（前端 SSE 的过程信号）。"""
    bus = FakeEventBus()
    gen_image_node(_state(), event_bus=bus, image_gateway=FakeImageGateway(_encode()), object_storage=FakeStorage())
    types = [payload["type"] for _, payload in bus.events]
    assert types == ["stage.imaging", "image.ready"]
    assert bus.events[0][0] == f"evt:{THREAD_ID}"


def test_uploaded_images_are_normalized_and_rehosted() -> None:
    """上传图：取回 → 规格化（ai_marked=False）→ 转存**新** key（绝不覆盖运营原图）。"""
    first, second = "https://oss.test/img/pa/a.png", "https://oss.test/img/pa/b.jpg"
    storage = FakeStorage({first: _encode("PNG", (700, 700)), second: _encode("JPEG", (800, 600))})
    normalizer = RecordingNormalizer(width=1000, height=1000, content_type="image/jpeg")
    out = gen_image_node(
        _state({"title": "T", "raw_images": [first, second]}),
        object_storage=storage,
        image_normalizer=normalizer,
        image_key_prefix="img/pa/test",
    )
    assert out["image_attached"] is True and out["image_error"] is None
    blocks = out["content_blocks"]
    assert len(blocks) == 3
    assert blocks[1]["url"].endswith("/upload-1.jpg") and blocks[2]["url"].endswith("/upload-2.jpg")
    assert blocks[1]["url"] != first and blocks[2]["url"] != second, "必须转存新对象，不得复用原图 key"
    assert (blocks[1]["width"], blocks[1]["height"]) == (1000, 1000)
    assert blocks[1]["source"] == "uploaded" and blocks[1]["alt"] == "T 商品图 1"
    assert normalizer.marks == [False, False], "实拍图绝不冒挂 AI 标识"
    assert all(content_type == "image/jpeg" for _, _, content_type in storage.puts)


def test_external_uploaded_url_falls_back_to_original() -> None:
    """第三方外链（不在本存储受管范围）→ 不取字节、不重编码，直接回退原 URL 且不写宽高。"""
    external = "https://third.example.com/pic.jpg"
    out = gen_image_node(
        _state({"title": "T", "raw_images": [external]}),
        object_storage=FakeStorage(),
        image_normalizer=RecordingNormalizer(),
    )
    block = out["content_blocks"][1]
    assert block["url"] == external and block["source"] == "uploaded"
    assert "width" not in block and "height" not in block
    assert out["image_attached"] is True and out["image_error"] is None


def test_uploaded_partial_failure_keeps_other_images() -> None:
    """单张失败不影响其余图片（best-effort）：失败用原 URL，成功用新 key。"""
    first, second = "https://oss.test/img/pa/a.png", "https://oss.test/img/pa/b.png"
    storage = FakeStorage({second: _encode("PNG")})
    out = gen_image_node(_state({"title": "T", "raw_images": [first, second]}), object_storage=storage, image_normalizer=RecordingNormalizer())
    blocks = out["content_blocks"]
    assert blocks[1]["url"] == first, "取不回的第一张用原 URL"
    assert blocks[2]["url"].endswith("/upload-2.jpg")
    assert out["image_attached"] is True


def test_uploaded_get_bytes_error_is_recorded_and_falls_back() -> None:
    """取回抛异常（网络故障）→ 记录原因并回退原 URL，不阻断其余图片。"""
    url = "https://oss.test/img/pa/a.png"
    out = gen_image_node(
        _state({"title": "T", "raw_images": [url]}),
        object_storage=FakeStorage(get_error=RuntimeError("网络中断")),
        image_normalizer=RecordingNormalizer(),
    )
    assert out["content_blocks"][1]["url"] == url
    assert "取回失败" in out["image_error"] and "网络中断" in out["image_error"]


def test_uploaded_without_normalizer_keeps_original_urls() -> None:
    """回归红线：未注入规格化器时上传图行为与接入前一致（直拼原 URL、无宽高）。"""
    url = "https://oss.test/img/pa/a.png"
    out = gen_image_node(_state({"title": "T", "raw_images": [url]}), object_storage=FakeStorage({url: _encode()}), image_normalizer=None)
    block = out["content_blocks"][1]
    assert block["url"] == url and block["source"] == "uploaded"
    assert "width" not in block and "height" not in block


def test_uploaded_without_storage_keeps_original_urls() -> None:
    """未配置对象存储时上传图直拼原 URL（不报错、不降级为空）。"""
    url = "https://oss.test/img/pa/a.png"
    out = gen_image_node(_state({"title": "T", "raw_images": [url]}), object_storage=None, image_normalizer=RecordingNormalizer())
    assert out["content_blocks"][1]["url"] == url and out["image_attached"] is True


def test_uploaded_images_win_over_ai_generation() -> None:
    """有上传图时**不调用**生图网关（零 AI 成本，也是合规上更稳的路径）。"""
    gateway = FakeImageGateway(_encode())
    url = "https://oss.test/img/pa/a.png"
    gen_image_node(_state({"title": "T", "raw_images": [url]}), image_gateway=gateway, object_storage=FakeStorage())
    assert gateway.calls == []


def test_generated_content_and_prompt_carry_product_facts() -> None:
    """生图提示词必须包含商品事实（题目/卖点），且按注入尺寸请求。"""
    gateway = FakeImageGateway(_encode())
    gen_image_node(
        _state({"title": "保温杯", "selling_points": ["24h 保温"], "base_price": 99}),
        image_gateway=gateway,
        object_storage=FakeStorage(),
        image_size="1280x1280",
    )
    prompt, size = gateway.calls[0]
    assert "保温杯" in prompt and "24h 保温" in prompt and size == "1280x1280"


def test_graph_wires_image_ports_end_to_end() -> None:
    """图级接线回归：build_workflow 注入的 gateway/storage/normalizer 必须真的到达 image 节点。

    为什么单独测：node 单测只覆盖节点本身，而「参数经 graph/workflow.py 的 partial 注入」
    是另一处易漏点（历史踩过 event_bus 漏注入导致前端看不到流式输出）。
    """
    from src.workflowcore.graph import build_workflow, new_memory_checkpointer

    storage = FakeStorage()
    normalizer = RecordingNormalizer(width=1000, height=1000, content_type="image/jpeg")
    workflow = build_workflow(
        checkpointer=new_memory_checkpointer(),
        image_gateway=FakeImageGateway(_encode("PNG")),
        object_storage=storage,
        image_normalizer=normalizer,
        asset_key_prefix="img/pa/test",
    )
    state = _state({"title": "平价商品", "base_price": 19.9})
    out = workflow.invoke(state.model_dump(), config=workflow.thread_config(THREAD_ID))

    blocks = out["content_blocks"]
    assert out.get("status") == "succeeded"
    assert blocks[0]["type"] == "text" and blocks[1]["type"] == "image"
    assert (blocks[1]["width"], blocks[1]["height"]) == (1000, 1000)
    assert blocks[1]["url"].endswith("cover-1.jpg") and blocks[1]["source"] == "ai_generated"
    assert normalizer.marks == [True], "AI 产物在链路里也必须带 AI 标识"
