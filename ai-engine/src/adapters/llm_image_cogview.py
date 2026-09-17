"""智谱 CogView / GLM-Image 生图 adapter（OpenAI 兼容 images/generations，单供应商基线）。

流程:
    1. POST {base_url}/images/generations → data[0].url（智谱短时效图床）；
    2. 立即 GET 该 URL 下载图片字节，交由调用方经 ObjectStorageServer(OSS) 转存自有 bucket，
       不将第三方临时 URL 直接落库或下发。

环境变量:
    - LLM_PROVIDER：为 zhipu 时启用；mock 表示回退内存 mock。
    - ZHIPU_API_KEY：必填；缺失时工厂返回 None（回退 mock）。
    - ZHIPU_BASE_URL：兼容网关基址（不含 /images/generations）。
    - ZHIPU_IMAGE_MODEL：生图模型，必填；缺失且未显式传 gen_model 时工厂抛 ValueError（不内置默认）。
    - ZHIPU_IMAGE_WATERMARK：1/true/yes/on 表示**保留**平台水印；其余（含未配置）为关闭。

水印与合规（重要）:
    · watermark_enabled=false 会同时关闭平台的「显式水印」与「隐式数字水印」；按《人工智能
      生成合成内容标识办法》第九条，标识义务随之转移到使用者一侧 —— 本项目由
      adapters/image_pillow.py 在规格化时对 AI 产物写入 AI 生成标识元数据来履行；
    · 关闭水印的**账户前提**是已在智谱控制台签署免责声明（个人中心-安全管理-去水印管理）；
      未签署的账号传 false 会被平台拒绝，此时 node_image 会按「无图降级」处理（记 image_error）；
    · 反之把 ZHIPU_IMAGE_WATERMARK 打开（保留平台水印）时，**不应**再让 Pillow 重编码 AI 产物
      （重编码会丢弃元数据，可能触及「不得隐匿标识」条款）。

尺寸:
    size 必须与模型支持的规格匹配（glm-image 与 cogview 系规则不同），否则网关可能静默回退
    到默认尺寸（历史「尺寸不一致」根因之一）。本模块用 resolve_image_size() 做显式校验与回退，
    实际出图尺寸以 node_image 对图片字节的**实测值**为准（见 workflowcore/node/image_probe.py）。

依赖:
    复用 llm_zhipu 的 DNS 预检 + 超时 urllib 传输，不新增第三方 HTTP 依赖。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from ..ports import LLMImageGenGateway
from . import llm_zhipu
from .llm_zhipu import LLMZhipuError, _raise_for_url_error

logger = logging.getLogger(__name__)

# glm-image 官方推荐尺寸枚举。
# 自定义尺寸规则：长宽 1024-2048px、均为 32 的整数倍、总像素不超过 2^22。
_GLM_IMAGE_SIZES = frozenset(
    {"1280x1280", "1568x1056", "1056x1568", "1472x1088", "1088x1472", "1728x960", "960x1728"}
)
_GLM_IMAGE_DEFAULT = "1280x1280"
_GLM_IMAGE_CUSTOM = (1024, 2048, 32, 2 ** 22)

# cogview 系（cogview-4 / cogview-4-250304 / cogview-3-flash 等）官方推荐尺寸枚举。
# 自定义尺寸规则：长宽 512-2048px、均被 16 整除、总像素不超过 2^21。
_COGVIEW_SIZES = frozenset(
    {"1024x1024", "768x1344", "864x1152", "1344x768", "1152x864", "1440x720", "720x1440"}
)
_COGVIEW_DEFAULT = "1024x1024"
_COGVIEW_CUSTOM = (512, 2048, 16, 2 ** 21)

# 默认生图请求尺寸（与 node_image.AI_IMAGE_SIZE 对齐；实际输出由规格化统一为目标规格）。
DEFAULT_REQUEST_SIZE = "1024x1024"


def _split_size(size: str) -> tuple[int | None, int | None]:
    """解析 "1280x1280" → (1280, 1280)；非法返回 (None, None)。

    参数:
        size: 形如 "1280x1280" 的尺寸串。
    返回:
        (width, height)；解析失败返回 (None, None)。
    """
    try:
        width_raw, height_raw = str(size).strip().lower().split("x")
        return int(width_raw), int(height_raw)
    except (ValueError, AttributeError, TypeError):
        return None, None


def resolve_image_size(model: str, size: str) -> str:
    """把请求尺寸收敛为该模型支持的合法规格（推荐枚举或合法自定义），否则回退默认尺寸。

    为什么必须做：glm-image 的默认/推荐尺寸是 1280x1280，且自定义规则（1024-2048、32 的整数倍）
    与 cogview 系（512-2048、16 的整除）不同；传一条对某模型非法/非推荐的尺寸时，
    网关可能静默改用默认尺寸 → 出图尺寸与调用方预期不一致（历史「尺寸不一致」根因）。

    参数:
        model: 生图模型名；含 "glm-image" 按 glm-image 规则，其余按 cogview 系规则。
        size: 请求尺寸（形如 "1024x1024"）；空/非法时回退该模型默认尺寸。
    返回:
        可直接发给网关的尺寸字符串（恒为合法值）。
    注意:
        回退时会记 warning 日志，便于排查「为什么出的图不是我要的尺寸」。
    """
    model_name = (model or "").strip().lower()
    if "glm-image" in model_name:
        recommended, fallback = _GLM_IMAGE_SIZES, _GLM_IMAGE_DEFAULT
        min_side, max_side, step, max_pixels = _GLM_IMAGE_CUSTOM
    else:
        recommended, fallback = _COGVIEW_SIZES, _COGVIEW_DEFAULT
        min_side, max_side, step, max_pixels = _COGVIEW_CUSTOM

    normalized = str(size or "").strip().lower()
    width, height = _split_size(normalized)
    if width is None:
        return fallback
    if normalized in recommended:
        return normalized
    if (
        min_side <= width <= max_side
        and min_side <= height <= max_side
        and width % step == 0
        and height % step == 0
        and width * height <= max_pixels
    ):
        return normalized
    logger.warning(
        "生图尺寸 %s 不满足模型 %s 的规格（推荐 %s），已回退为 %s",
        normalized or "(空)", model, sorted(recommended)[:3], fallback,
    )
    return fallback


def _truthy(raw: str | None, *, default: bool) -> bool:
    """把环境变量收敛为布尔：未配置/空白用 default，1/true/yes/on 为真，其余为假。

    参数:
        raw: 环境变量原始值。
        default: 未配置时的取值。
    返回:
        布尔值。
    """
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_image_cogview_gateway_from_env(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    gen_model: str | None = None,
    watermark: bool | None = None,
) -> LLMImageCogviewGateway | None:
    """按入参/环境变量构建智谱生图 Gateway；未配置时返回 None，由上层回退 mock。

    参数:
        provider / api_key / base_url / gen_model: 显式覆盖值；为 None 时分别读取
            LLM_PROVIDER / ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_IMAGE_MODEL。
            gen_model 无内置默认：与 ZHIPU_IMAGE_MODEL 二者至少需有一个。
        watermark: 是否保留平台水印；None 时读 ZHIPU_IMAGE_WATERMARK
            （未配置即**关闭水印**：默认依赖已签署的免责声明，见模块 docstring）。
    返回:
        LLMImageCogviewGateway 实例；None 表示未配置（回退内存 mock）。
    异常:
        ValueError: 已选定供应商但 ZHIPU_BASE_URL 或生图模型缺失（快速失败，
            避免把 model=null 发到网关后被误判为网络 / 鉴权故障）。
    """
    provider = provider if provider is not None else os.getenv("LLM_PROVIDER", "")
    if provider == "mock":
        return None
    key = api_key if api_key is not None else os.getenv("ZHIPU_API_KEY")
    if not key:
        return None
    if provider and provider != "zhipu":
        # 当前仅支持单个供应商。
        return None
    resolved_base = base_url or os.getenv("ZHIPU_BASE_URL")
    if not resolved_base:
        raise ValueError("智谱生图需要 ZHIPU_BASE_URL")
    # 模型无内置默认：缺失即快速失败，避免把 model=null 发到网关（生图必失败且难排查）。
    resolved_model = gen_model or os.getenv("ZHIPU_IMAGE_MODEL")
    if not resolved_model:
        raise ValueError("智谱生图需要 ZHIPU_IMAGE_MODEL（或显式传 gen_model）")
    resolved_watermark = (
        watermark if watermark is not None else _truthy(os.getenv("ZHIPU_IMAGE_WATERMARK"), default=False)
    )
    return LLMImageCogviewGateway(
        api_key=key,
        base_url=resolved_base,
        model=resolved_model,
        watermark=resolved_watermark,
    )


class LLMImageCogviewGateway(LLMImageGenGateway):
    """智谱 CogView / GLM-Image 同步实现（端口签名为同步协议）。

    流程：images/generations 取短时效图床 URL → 立即下载字节返回，绝不把第三方
    临时 URL 直接落库或下发；失败抛 LLMZhipuError，由 node_image 节点按无图降级处理。
    """

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 timeout: float = 120.0, watermark: bool = False) -> None:
        """初始化同步 Gateway。

        参数:
            api_key: 智谱 API Key，必填。
            base_url: 兼容网关基址（末尾斜杠会被去除）。
            model: 生图模型（必填；工厂已保证非空）。
            timeout: 单次 HTTP 请求超时（秒）。
            watermark: 是否保留平台水印（**默认关闭**，需账户已签免责声明）。
        异常:
            ValueError: api_key 为空。
        """
        if not api_key:
            raise ValueError("LLMImageCogviewGateway 需要 api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.watermark = bool(watermark)

    def _request(self, url: str, payload: dict | None) -> urllib.request.Request:
        """构造 HTTP 请求：传 payload 走 POST(JSON)，不传则走 GET（下载图床资源）。

        参数:
            url: 目标 URL。
            payload: 请求体；None 表示 GET 下载。
        返回:
            已就绪的请求对象（含 Bearer 鉴权头）。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if payload is None:
            headers["Accept"] = "image/*"          # 下载图床资源
            data = None
        else:
            headers["Accept"] = "application/json"
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(url, data=data, headers=headers,
                                      method="POST" if data is not None else "GET")

    def build_payload(self, prompt: str, size: str) -> dict:
        """构造 images/generations 请求体（独立成方法以便单测断言，无需真实 HTTP）。

        参数:
            prompt: 用户提示词（商品信息 + 风格约束）。
            size: 目标尺寸（会经 resolve_image_size 收敛为该模型的合法规格）。
        返回:
            请求体 dict：model / prompt / size / n=1 / watermark_enabled。
        """
        return {
            "model": self.model,
            "prompt": prompt,
            "size": resolve_image_size(self.model, size),
            "n": 1,
            # 关闭平台显式+隐式水印（账户须已签免责声明）；标识义务由本项目的
            # image_pillow 适配器写入 AI 标识元数据履行（见模块 docstring）。
            "watermark_enabled": self.watermark,
        }

    def generate_image(self, prompt: str, *, size: str = DEFAULT_REQUEST_SIZE) -> bytes:
        """生成配图：调用 images/generations 取图床 URL，再下载其字节返回。

        参数:
            prompt: 用户提示词（商品信息 + 风格约束）。
            size: 图片尺寸（默认 1024x1024；会按模型规格收敛，见 resolve_image_size）。
        返回:
            图片字节（png/jpeg）；由调用方经 ObjectStorageServer 转存 OSS。
        异常:
            LLMZhipuError: 网络 / HTTP 错误，或响应结构缺少 data[0].url。
        """
        payload = self.build_payload(prompt, size)
        # 生图请求：POST /images/generations。
        gen_req = self._request(f"{self.base_url}/images/generations", payload)
        try:
            with llm_zhipu._urlopen(gen_req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as exc:
            # 复用 llm_zhipu 的统一映射：HTTPError 报状态码与响应体，其余报网络原因。
            _raise_for_url_error(exc)

        # images/generations 响应字段：data[0].url（智谱短时效图床）。
        try:
            image_url = body["data"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMZhipuError(
                f"智谱生图响应缺少 data[0].url: {str(body)[:200]}"
            ) from exc

        # 图床为短时效资源：立即下载字节，交由调用方转存 OSS。
        download_req = self._request(image_url, None)
        try:
            with llm_zhipu._urlopen(download_req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            raise LLMZhipuError(f"下载智谱生图失败: {exc.reason}") from exc


__all__ = [
    "DEFAULT_REQUEST_SIZE",
    "LLMImageCogviewGateway",
    "build_image_cogview_gateway_from_env",
    "resolve_image_size",
]
