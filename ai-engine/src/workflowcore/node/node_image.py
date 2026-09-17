"""image 节点：图文详情「配图」。

  · 有上传图（raw_images）→ 逐张取回字节 → 规格化（统一 1:1/格式）→ 转存自有 OSS → 拼 image block；
    取不回（第三方外链 / 网络失败）→ 回退原 URL 直拼（best-effort，不阻断）；
  · 无上传图 → 调 LLMImageGenGateway 生一张图 → 规格化 → 落自有 OSS → 拼 image block；
  · 未注入 ImageNormalizer → 保持历史行为（不重编码），尺寸改用探测值；
  · 失败一律降级：content_blocks 只含 text，image_error 记录，绝不阻断落库/HITL
    （不让一张图毁掉已通过文案）。

尺寸口径（元数据以字节事实为准）:
    历史缺陷：block 的 width/height 直接写「请求尺寸」（如 1024x1024），而网关/模型实际
    出图尺寸可能不同（glm-image 默认 1280x1280）→ 前端按假尺寸渲染即「尺寸不一致」。
    现在：注入规格化器 → 用规格化后的**实测输出尺寸**；未注入 → 用 probe_image_size()
    读原始字节的真实尺寸；两者都取不到 → 不写 width/height（**绝不写请求值**）。

Content-Type 与对象键:
    put_bytes 的 Content-Type 必须与字节真实格式一致（OSS SigV1 预签名把它算进签名串，
    adapters/oss.py::_presign），对象键后缀也随之一致：cover-1.jpg / upload-2.png
    （历史实现恒传 image/png，网关返回 JPEG 时会出现「扩展名/类型与字节不符」）。

合规:
    上传图按实拍图处理（ai_marked=False —— 绝不冒挂 AI 标识）；
    AI 生图产物 ai_marked=True（规格化时写入 AI 生成标识元数据，履行标识义务）。
    图不进 LLM 评估：评估器只评 generated_content 文本（图-文解耦，见 README 模型选型）。
"""
from __future__ import annotations

from typing import Any

from ...ports import (
    ImageNormalizer,
    LLMImageGenGateway,
    ObjectStorageServer,
    file_extension_for_content_type,
)
from ..state import ListingState, ensure_state
from .image_probe import probe_content_type, probe_image_size

# AI 配图请求尺寸：只决定向生图网关请求的规格（会被 llm_image_cogview.resolve_image_size()
# 按模型收敛为合法规格），**落库尺寸**由 ImageNormalizer 统一为 IMAGE_NORMALIZE_SIZE（默认 1000x1000）。
AI_IMAGE_SIZE = "1024x1024"

# 未识别 MIME 时的兜底 Content-Type（与 put_bytes 端口默认值一致）
_FALLBACK_CONTENT_TYPE = "image/png"


def _build_image_prompt(info: dict[str, Any]) -> str:
    """生图提示词：只用商品事实（与流式文案解耦），并约束画面内不出现文字/水印。

    注意:
        此处「不出现水印」约束的是**画面内容**（模型自己画上去的水印文字）；
        平台侧水印由 ZHIPU_IMAGE_WATERMARK 控制（默认关闭），两者互不替代。

    参数:
        info: 商品素材 dict；读 title / selling_points(selling_point) / base_price。
    返回:
        生图提示词字符串。
    """
    title = str(info.get("title") or "商品")
    selling = info.get("selling_points") or info.get("selling_point") or ""
    desc = "；".join(str(s) for s in selling) if isinstance(selling, list) else str(selling or "")
    price = info.get("base_price")
    parts = [f"为商品「{title}」生成一张电商详情配图。"]
    if desc:
        parts.append(f"可表现的卖点：{desc}")
    if price:
        parts.append(f"参考价位：{price}")
    parts.append("画面写实、构图干净，符合电商图文详情观感；不出现任何文字、LOGO、水印或广告极限用语。")
    return "".join(parts)


def _uploaded_image_urls(info: dict[str, Any]) -> list[str]:
    """从 raw_product_info 取运营上传图（products.raw_images 的 OSS 公有 URL 列表）。

    参数:
        info: 商品素材 dict；读 raw_images（list[str]；防御性接受单个 URL 字符串）。
    返回:
        过滤掉非字符串/空串后的 URL 列表；无上传图返回 []（调用方据此走 AI 生图分支）。
    """
    raw = info.get("raw_images") or []
    if isinstance(raw, str):  # 防御：CSV/手造数据可能给单 URL 字符串
        raw = [raw]
    return [u for u in raw if isinstance(u, str) and u]


def _image_block(*, url: str, alt: str, source: str, width: int | None, height: int | None) -> dict:
    """组装 image block（对齐 product_contents.content_data 契约）。

    参数:
        url: 图片 URL（自有 OSS 或第三方外链）。
        alt: 无障碍/占位文案。
        source: 来源标记（uploaded = 运营实拍图；ai_generated = AI 生图产物，供审计与平台申报）。
        width: **实测**宽度；None 表示未知（不写该字段，绝不写请求尺寸）。
        height: **实测**高度；None 表示未知。
    返回:
        image block dict：{type, url, alt, source[, width, height]}。
    """
    block: dict[str, Any] = {"type": "image", "url": url, "alt": alt, "source": source}
    if width and height:
        block["width"], block["height"] = int(width), int(height)
    return block


def _normalize_or_probe(
    data: bytes,
    image_normalizer: ImageNormalizer | None,
    *,
    ai_marked: bool,
) -> tuple[bytes, str, int | None, int | None]:
    """把图片字节规格化（未注入规格化器时改为探测原字节）。

    参数:
        data: 原始图片字节。
        image_normalizer: 规格化端口；None 时只做探测（保持历史行为）。
        ai_marked: 是否写入 AI 生成标识元数据；**仅 AI 生图产物为 True**，
            运营实拍图必须为 False。
    返回:
        (待上传字节, Content-Type, 实测宽, 实测高)；探测失败时宽高为 None。
    异常:
        ImageNormalizationError: 规格化失败（由调用方按降级/回退处理）。
    """
    if image_normalizer is not None:
        normalized = image_normalizer.normalize(data, ai_marked=ai_marked)
        return normalized.data, normalized.content_type, normalized.width, normalized.height
    width, height = probe_image_size(data)
    return data, (probe_content_type(data) or _FALLBACK_CONTENT_TYPE), width, height


def _build_uploaded_block(
    *,
    url: str,
    idx: int,
    alt: str,
    state: ListingState,
    image_normalizer: ImageNormalizer | None,
    object_storage: ObjectStorageServer | None,
    image_key_prefix: str,
    errors: list[str],
) -> dict:
    """上传图 block：优先「取回字节 → 规格化 → 转存自有 OSS」，失败一律回退原 URL 直拼。

    参数:
        url: 运营上传图 URL（products.raw_images）。
        idx: 第几张（从 1 起；用于对象键与提示信息）。
        alt: 无障碍文案。
        state: 当前图状态（用 org_id / product_id / thread_id 组织对象键）。
        image_normalizer: 规格化端口；None 时不重编码（直接用原 URL）。
        object_storage: 对象存储；None 时不转存（直接用原 URL）。
        image_key_prefix: 对象键前缀（worker 注入 img/pa/{env}）。
        errors: 失败原因收集列表（节点汇总进 image_error，保持 str 契约）。
    返回:
        image block dict：转存成功用新 URL + 实测宽高；回退时用原 URL 且**不写宽高**
        （没取到字节就没有事实尺寸，绝不编造）。
    注意:
        · 只对本存储受管对象取字节（ObjectStorageServer.get_bytes 契约）：第三方外链直接
          回退原 URL —— 既避免 SSRF，也避免「替第三方抓图」；
        · 单张失败不影响其余图片（best-effort），原因汇入 image_error。
    """
    if image_normalizer is None or object_storage is None:
        return _image_block(url=url, alt=alt, source="uploaded", width=None, height=None)
    try:
        data = object_storage.get_bytes(url)
    except Exception as exc:  # noqa: BLE001 取回失败：回退原 URL，不阻断其余图片
        errors.append(f"上传图 {idx} 取回失败，已用原图直链: {exc!r}")
        return _image_block(url=url, alt=alt, source="uploaded", width=None, height=None)
    if not data:
        # 非本存储受管对象（第三方外链）或对象不存在：不重编码，直接用原 URL
        return _image_block(url=url, alt=alt, source="uploaded", width=None, height=None)
    try:
        payload, content_type, width, height = _normalize_or_probe(data, image_normalizer, ai_marked=False)
        ext = file_extension_for_content_type(content_type)
        # 新对象键与源图 key 不同：绝不覆盖运营原图（原图可能被其它位置引用）
        key = f"{image_key_prefix}/{state.org_id}/{state.product_id}/{state.thread_id}/upload-{idx}.{ext}"
        new_url = object_storage.put_bytes(key, payload, content_type=content_type)
        return _image_block(url=new_url, alt=alt, source="uploaded", width=width, height=height)
    except Exception as exc:  # noqa: BLE001 规格化/转存失败：回退原 URL
        errors.append(f"上传图 {idx} 规格化/转存失败，已用原图直链: {exc!r}")
        return _image_block(url=url, alt=alt, source="uploaded", width=None, height=None)


def gen_image_node(
    state: ListingState,
    event_bus=None,
    image_gateway: LLMImageGenGateway | None = None,
    object_storage: ObjectStorageServer | None = None,
    image_normalizer: ImageNormalizer | None = None,
    image_key_prefix: str = "img/pa",
    image_size: str = AI_IMAGE_SIZE,
) -> dict:
    """配图节点：把 text block 与图片 block 组装为 content_blocks（落库/快照唯一事实源）。

    参数:
        state: 当前图状态；读 raw_product_info（title/selling_points/raw_images）与 generated_content。
        event_bus: 事件总线；None 时不发 stage.imaging / image.ready 事件。
        image_gateway: AI 生图网关；None 或 object_storage 为 None 时降级纯文本。
        object_storage: 对象存储；生图产物与规格化后的上传图经其落自有 bucket 并取永久 URL。
        image_normalizer: 图片规格化端口；None 时保持历史行为（不重编码，仅探测实测尺寸）。
        image_key_prefix: 对象键前缀，由调用方注入（worker 侧传 img/pa/{env}）。
        image_size: 向生图网关请求的尺寸（默认 1024x1024；按模型规格收敛，落库尺寸由
            image_normalizer 统一为 IMAGE_NORMALIZE_SIZE）。
    返回:
        增量 dict：{"content_blocks": [...], "image_attached": bool, "image_error": str|None}。
    注意:
        · 失败一律降级：绝不因配图失败阻断文案落库/HITL（宁可纯文本，也不丢已通过文案）；
        · width/height 只写**实测**值（规格化输出尺寸或原始字节探测值），绝不写请求尺寸；
        · image_error 保持 str 契约（多条原因用 "; " 拼接），避免 State 契约扩散。
    """
    state = ensure_state(state)
    info: dict[str, Any] = state.raw_product_info or {}
    title = str(info.get("title") or "商品")
    blocks: list[dict] = [{"type": "text", "text": state.generated_content or ""}]
    image_attached = False
    errors: list[str] = []

    uploaded = _uploaded_image_urls(info)
    if uploaded:
        # 有运营上传图：逐张规格化重落（零生图成本；取不回则回退原 URL）
        if event_bus is not None:
            event_bus.publish(
                f"evt:{state.thread_id}",
                {"type": "stage.imaging", "data": {"source": "uploaded", "count": len(uploaded), "product_id": state.product_id}},
            )
        for idx, url in enumerate(uploaded, start=1):
            blocks.append(
                _build_uploaded_block(
                    url=url,
                    idx=idx,
                    alt=f"{title} 商品图 {idx}",
                    state=state,
                    image_normalizer=image_normalizer,
                    object_storage=object_storage,
                    image_key_prefix=image_key_prefix,
                    errors=errors,
                )
            )
        image_attached = True
    else:
        # 无上传图 → AI 生图 + 规格化 + 落自有 OSS（缺 gateway/storage = 优雅降级为纯文本）
        if event_bus is not None:
            event_bus.publish(
                f"evt:{state.thread_id}",
                {"type": "stage.imaging", "data": {"source": "ai", "product_id": state.product_id}},
            )
        if image_gateway is None or object_storage is None:
            errors.append("未配置生图模型/对象存储，本次以纯文本落库")
        else:
            try:
                data = image_gateway.generate_image(_build_image_prompt(info), size=image_size)
                # ai_marked=True：AI 产物写入 AI 生成标识元数据（平台水印已按默认关闭）
                payload, content_type, width, height = _normalize_or_probe(data, image_normalizer, ai_marked=True)
                ext = file_extension_for_content_type(content_type)
                # 对象键带 thread_id：同商品多轮生成互不覆盖；img/pa 前缀供删除白名单/生命周期识别
                key = f"{image_key_prefix}/{state.org_id}/{state.product_id}/{state.thread_id}/cover-1.{ext}"
                url = object_storage.put_bytes(key, payload, content_type=content_type)
                block = _image_block(url=url, alt=f"{title} AI 配图", source="ai_generated", width=width, height=height)
                blocks.append(block)
                image_attached = True
                if event_bus is not None:
                    event_bus.publish(
                        f"evt:{state.thread_id}",
                        {"type": "image.ready", "data": {"url": url, "alt": block["alt"], "source": "ai_generated", "product_id": state.product_id}},
                    )
            except Exception as exc:  # noqa: BLE001 生图/规格化/上传失败：降级纯文本，绝不阻断文案链路
                errors.append(f"AI 配图失败，已按纯文本落库: {exc!r}")
    # Langgraph自动存储更新ListingState的对应字段
    return {
        "content_blocks": blocks,
        "image_attached": image_attached,
        "image_error": "; ".join(errors) if errors else None,
    }
