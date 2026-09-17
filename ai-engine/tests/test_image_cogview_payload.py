"""智谱生图 adapter 单测（monkeypatch 传输层；不需要真实 HTTP / API Key / OSS）。

覆盖:
  · 请求体契约：必含 watermark_enabled（**默认 False = 关闭平台显式+隐式水印**）
    与按模型收敛后的 size；
  · 尺寸与模型匹配：glm-image（默认 1280x1280；自定义 1024-2048 且 32 的整数倍）与
    cogview 系（默认 1024x1024；自定义 512-2048 且被 16 整除）的推荐枚举、合法自定义与回退；
  · 工厂：默认关闭水印、可由 ZHIPU_IMAGE_WATERMARK 打开；缺 Key 返回 None、缺模型快速失败；
  · 下载流程：先 POST /images/generations 取图床 URL，再 GET 下载字节返回
    （绝不把第三方临时 URL 直接落库/下发）。

为什么要有这组用例:
    · 水印开关是合规相关的一行参数（关水印后标识义务转移到我方，由 image_pillow 写 AI 标识履行），
      必须锁死默认值与可观测性；
    · 尺寸与模型不匹配会让网关静默回退默认尺寸 —— 这是历史「出图尺寸不一致」的根因之一。
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from src.adapters import llm_zhipu
from src.adapters.llm_image_cogview import (
    LLMImageCogviewGateway,
    build_image_cogview_gateway_from_env,
    resolve_image_size,
)
from src.adapters.llm_zhipu import LLMZhipuError


class _FakeResponse:
    """最小 HTTP 响应替身（支持 `with ... as resp` 与 read()）。"""

    def __init__(self, body: bytes) -> None:
        """初始化。

        参数:
            body: 响应体字节。
        """
        self._body = body

    def read(self, *args) -> bytes:
        """返回整个响应体。

        参数:
            *args: 占位（真实 urllib 允许传读取长度，本替身忽略）。
        返回:
            响应体字节。
        """
        return self._body

    def __enter__(self) -> "_FakeResponse":
        """进入 with 块。

        返回:
            self。
        """
        return self

    def __exit__(self, *exc) -> bool:
        """退出 with 块。

        参数:
            *exc: 异常三元组（本替身不处理）。
        返回:
            False（不吞异常）。
        """
        return False


def _gateway(model: str = "glm-image", watermark: bool = False) -> LLMImageCogviewGateway:
    """构造被测网关（不发起网络请求）。

    参数:
        model: 生图模型。
        watermark: 是否保留平台水印。
    返回:
        LLMImageCogviewGateway 实例。
    """
    return LLMImageCogviewGateway(
        api_key="k", base_url="https://api.test/api/paas/v4", model=model, watermark=watermark
    )


def test_payload_disables_watermark_by_default() -> None:
    """默认必须携带 watermark_enabled=False（账户已签免责声明，标识义务由我方履行）。"""
    payload = _gateway().build_payload("提示词", "1024x1024")
    assert payload["watermark_enabled"] is False
    assert payload["model"] == "glm-image" and payload["n"] == 1 and payload["prompt"] == "提示词"


def test_payload_keeps_watermark_when_enabled() -> None:
    """显式开启时携带 True（保留平台隐式水印；此时不得再让 Pillow 重编码）。"""
    assert _gateway(watermark=True).build_payload("p", "1024x1024")["watermark_enabled"] is True


def test_payload_carries_resolved_size() -> None:
    """请求体里的 size 必须是按模型收敛后的值。"""
    assert _gateway("glm-image").build_payload("p", "100x100")["size"] == "1280x1280"
    assert _gateway("cogview-4").build_payload("p", "999x999")["size"] == "1024x1024"


@pytest.mark.parametrize(
    "model,requested,expected",
    [
        ("glm-image", "1280x1280", "1280x1280"),
        ("glm-image", "1568x1056", "1568x1056"),
        ("glm-image", "1728x960", "1728x960"),
        ("glm-image", "1024x1024", "1024x1024"),
        ("glm-image", "1152x1152", "1152x1152"),
        ("glm-image", "1024x1040", "1280x1280"),
        ("glm-image", "100x100", "1280x1280"),
        ("glm-image", "4096x4096", "1280x1280"),
        ("glm-image", "", "1280x1280"),
        ("glm-image", "garbage", "1280x1280"),
        ("cogview-4", "1024x1024", "1024x1024"),
        ("cogview-4", "1440x720", "1440x720"),
        ("cogview-4", "720x1440", "720x1440"),
        ("cogview-4", "800x800", "800x800"),
        ("cogview-4", "900x900", "1024x1024"),
        ("cogview-4", "500x500", "1024x1024"),
        ("cogview-3-flash", "768x1344", "768x1344"),
    ],
)
def test_resolve_image_size_matches_model_rules(model: str, requested: str, expected: str) -> None:
    """尺寸解析矩阵：推荐枚举原样通过、合法自定义通过、越界/非法回退模型默认值。"""
    assert resolve_image_size(model, requested) == expected


def test_resolve_image_size_tolerates_whitespace_and_case() -> None:
    """大小写/空白容错（配置来自环境变量，容易被手抖写错）。"""
    assert resolve_image_size("glm-image", " 1280X1280 ") == "1280x1280"


def test_factory_defaults_to_watermark_off(monkeypatch) -> None:
    """工厂默认关闭水印（未配置 ZHIPU_IMAGE_WATERMARK 时）。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://api.test/v4")
    monkeypatch.setenv("ZHIPU_IMAGE_MODEL", "glm-image")
    monkeypatch.delenv("ZHIPU_IMAGE_WATERMARK", raising=False)
    gateway = build_image_cogview_gateway_from_env()
    assert gateway is not None and gateway.watermark is False


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True), ("ON", True), ("0", False), ("false", False), ("", False)])
def test_factory_reads_watermark_env(monkeypatch, raw: str, expected: bool) -> None:
    """ZHIPU_IMAGE_WATERMARK 的真值解析（空值视为关闭）。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://api.test/v4")
    monkeypatch.setenv("ZHIPU_IMAGE_MODEL", "glm-image")
    monkeypatch.setenv("ZHIPU_IMAGE_WATERMARK", raw)
    gateway = build_image_cogview_gateway_from_env()
    assert gateway is not None and gateway.watermark is expected


def test_factory_explicit_watermark_overrides_env(monkeypatch) -> None:
    """显式传参优先于环境变量（便于测试与多环境覆盖）。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://api.test/v4")
    monkeypatch.setenv("ZHIPU_IMAGE_MODEL", "glm-image")
    monkeypatch.setenv("ZHIPU_IMAGE_WATERMARK", "1")
    gateway = build_image_cogview_gateway_from_env(watermark=False)
    assert gateway is not None and gateway.watermark is False


def test_factory_returns_none_without_api_key(monkeypatch) -> None:
    """缺 API Key → None（上层回退内存 mock，不影响进程启动）。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    assert build_image_cogview_gateway_from_env() is None


def test_factory_mock_provider_returns_none(monkeypatch) -> None:
    """provider=mock 显式关闭真实生图。"""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    assert build_image_cogview_gateway_from_env() is None


def test_factory_raises_without_model(monkeypatch) -> None:
    """已选定供应商但缺模型 → 快速失败（避免 model=null 被误判为网络故障）。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://api.test/v4")
    monkeypatch.delenv("ZHIPU_IMAGE_MODEL", raising=False)
    with pytest.raises(ValueError):
        build_image_cogview_gateway_from_env()


def test_generate_image_posts_payload_then_downloads(monkeypatch) -> None:
    """端到端（假传输）：POST 生图取图床 URL → GET 下载字节返回，且请求体含水印/尺寸参数。"""
    seen = []

    def fake_urlopen(request, timeout=None):
        """替代 llm_zhipu._urlopen：第一次回生图响应，第二次回图片字节。

        参数:
            request: urllib Request。
            timeout: 超时（被忽略）。
        返回:
            _FakeResponse。
        """
        seen.append(request)
        if len(seen) == 1:
            return _FakeResponse(json.dumps({"data": [{"url": "https://img.test/a.png"}]}).encode("utf-8"))
        return _FakeResponse(b"\x89PNG\r\n\x1a\nIMAGE-BODY")

    monkeypatch.setattr(llm_zhipu, "_urlopen", fake_urlopen)
    data = _gateway("glm-image").generate_image("提示词", size="1024x1024")

    assert data == b"\x89PNG\r\n\x1a\nIMAGE-BODY", "必须返回下载到的图片字节（而非第三方 URL）"
    assert len(seen) == 2
    body = json.loads(seen[0].data.decode("utf-8"))
    assert body["watermark_enabled"] is False
    assert body["size"] == "1024x1024" and body["model"] == "glm-image"
    assert seen[0].full_url == "https://api.test/api/paas/v4/images/generations"
    assert seen[0].get_method() == "POST"
    assert seen[1].full_url == "https://img.test/a.png" and seen[1].get_method() == "GET"
    assert seen[0].headers["Authorization"] == "Bearer k"


def test_generate_image_raises_on_unexpected_response(monkeypatch) -> None:
    """响应缺 data[0].url → LLMZhipuError（由 node_image 降级处理）。"""
    monkeypatch.setattr(llm_zhipu, "_urlopen", lambda request, timeout=None: _FakeResponse(b"{}"))
    with pytest.raises(LLMZhipuError):
        _gateway().generate_image("p")


def test_generate_image_maps_network_error(monkeypatch) -> None:
    """网络错误统一映射为 LLMZhipuError（不向上层漏 urllib 类型）。"""
    def boom(request, timeout=None):
        """抛出 urllib 网络错误。

        参数:
            request: urllib Request。
            timeout: 超时（被忽略）。
        异常:
            urllib.error.URLError: 总是抛出。
        """
        raise urllib.error.URLError("dns down")

    monkeypatch.setattr(llm_zhipu, "_urlopen", boom)
    with pytest.raises(LLMZhipuError):
        _gateway().generate_image("p")


def test_generate_image_download_failure_raises(monkeypatch) -> None:
    """图床下载失败 → LLMZhipuError（绝不返回空字节冒充成功）。"""
    calls = []

    def fake_urlopen(request, timeout=None):
        """第一次成功回生图响应，第二次抛网络错误。

        参数:
            request: urllib Request。
            timeout: 超时（被忽略）。
        返回:
            _FakeResponse（仅第一次）。
        异常:
            urllib.error.URLError: 下载阶段抛出。
        """
        calls.append(request)
        if len(calls) == 1:
            return _FakeResponse(json.dumps({"data": [{"url": "https://img.test/a.png"}]}).encode("utf-8"))
        raise urllib.error.URLError("download timeout")

    monkeypatch.setattr(llm_zhipu, "_urlopen", fake_urlopen)
    with pytest.raises(LLMZhipuError):
        _gateway().generate_image("p")
