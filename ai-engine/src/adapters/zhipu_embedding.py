"""智谱 Embedding 真实 adapter（OpenAI 兼容 /embeddings，RAG/Milvus 语义召回用）。

职责:
    实现 EmbeddingGateway 端口：把一批文本编码为向量，供 Milvus RAG 做语义召回；
    只做「文本 → 向量」，不做召回、不承载合规判定。

流程:
    1. 组装 OpenAI 兼容请求体 {model, input, dimensions}；
    2. POST {base_url}/embeddings（传输复用 llm_zhipu 的 DNS 预检 + 超时 urllib）；
    3. 按响应 index 排序取 embedding，保证与入参顺序一一对应。

环境变量:
    - LLM_PROVIDER：为 mock 时不构造实例（RAG 显式关闭，等价空上下文）。
    - ZHIPU_API_KEY：必填（与 GLM 生文复用同一 Key）；缺失时工厂返回 None。
    - EMBEDDING_MODEL：必填；缺失时工厂返回 None（不内置回退，避免静默用错模型）。
    - EMBEDDING_DIM：向量维度，默认 1024；⚠ 必须与 Milvus 集合 dim 对齐
      （见 database/milvus/init_collections.py 与 MILVUS_DIM）。
    - EMBEDDING_BASE_URL：兼容网关基址（不含 /embeddings），默认智谱官方。

异常:
    LLMZhipuError：HTTP / 网络 / DNS / 响应格式异常；由上层捕获后降级（RAG 等价空上下文）。

依赖:
    仅标准库；复用 llm_zhipu 的 DNS 预检 + 超时 urllib 传输，保持 ai-engine 无新增第三方 HTTP 依赖。
"""


from __future__ import annotations

import json
import os
import urllib.request

from ..ports.embedding_gateway import EmbeddingGateway
from . import llm_zhipu
from .llm_zhipu import LLMZhipuError

# 智谱 OpenAI 兼容网关基址默认值：与 infra/.env 的 EMBEDDING_BASE_URL 保持一致。
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# 默认模型：当前锁定的单一供应商基线；默认维度：embedding-3 的常用输出维度。
DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_EMBEDDING_DIM = 1024


class ZhipuEmbeddingGateway(EmbeddingGateway):
    """智谱 embedding 同步实现；请求/响应异常抛 LLMZhipuError 供上层降级。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dim: int = DEFAULT_EMBEDDING_DIM,
        timeout: float = 60.0,
    ) -> None:
        """构造实例（必填项校验由工厂 build_embedding_gateway_from_env 完成）。

        参数:
            api_key: 智谱 API Key，用于 Bearer 鉴权。
            base_url: 兼容网关基址，不含 /embeddings；尾部斜杠会被规范化去掉。
            model: embedding 模型名（默认 embedding-3）。
            dim: 输出向量维度；⚠ 必须与 Milvus 集合 dim 一致，否则写入/检索维度不匹配。
            timeout: 单次 HTTP 请求超时（秒），默认 60s；由 llm_zhipu 的传输层统一生效。
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = int(dim)
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码为向量（顺序与入参一一对应）。

        参数:
            texts: 待编码文本列表；为空时直接返回 []，不发起 HTTP 请求。
        返回:
            list[list[float]]：与入参等长的向量列表（按响应 index 排序保证顺序对应）。
        异常:
            LLMZhipuError: HTTP / 网络 / DNS 失败，或响应缺少 data/embedding 字段（格式异常）。
        """
        items = list(texts)
        if not items:
            return []
        # input 支持批量：一次请求编码多条文本，减少往返次数
        # dimensions 必须与 Milvus 集合 dim 对齐（EMBEDDING_DIM / MILVUS_DIM）
        payload = {"model": self.model, "input": items, "dimensions": self.dim}
        # 拼接 OpenAI 兼容的 HTTP 请求（仅标准库，不引入 OpenAI SDK）
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            # 发起 HTTP 请求：走 llm_zhipu 模块属性（而非 import 进来的值），
            # 与 llm_zhipu.py 声明的「单测替换 _urlopen 注入假 HTTP」约定保持一致
            with llm_zhipu._urlopen(req, timeout=self.timeout) as resp:
                # 读取并解析 JSON 响应
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            # 传输层可能已抛 LLMZhipuError（DNS 预检失败等），保持异常类型与信息不二次包装
            if isinstance(exc, LLMZhipuError):
                raise
            raise LLMZhipuError(f"智谱 embedding 请求失败: {exc!r}") from exc
        try:
            # 取回向量：响应可能乱序，按 index 排序后与入参顺序一一对应
            rows = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            # 统一转成 LLMZhipuError，避免上层需要区分第三方响应结构差异
            raise LLMZhipuError(f"智谱 embedding 响应格式异常: {str(data)[:200]}") from exc


def build_embedding_gateway_from_env(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    dim: int | None = None,
) -> ZhipuEmbeddingGateway | None:
    """按入参/环境变量构建智谱 Embedding Gateway；未配置时返回 None（RAG 关闭）。

    参数:
        api_key / model / base_url / dim: 显式覆盖值；为 None 时分别读取
            ZHIPU_API_KEY / EMBEDDING_MODEL / EMBEDDING_BASE_URL / EMBEDDING_DIM。
    返回:
        ZhipuEmbeddingGateway 实例；None 表示未启用 embedding，调用方应关闭 RAG
        （等价空上下文），而不是报错。
    说明:
        以下任一情况返回 None：LLM_PROVIDER=mock（显式回退 mock）、缺少 ZHIPU_API_KEY、
        缺少 EMBEDDING_MODEL（避免静默用错模型，故不内置默认模型）。
    """
    provider = os.getenv("LLM_PROVIDER", "")
    key = api_key if api_key is not None else os.getenv("ZHIPU_API_KEY")
    resolved_model = model if model is not None else os.getenv("EMBEDDING_MODEL")
    if not key or provider == "mock" or not resolved_model:
        return None
    resolved_dim = dim if dim is not None else int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))
    return ZhipuEmbeddingGateway(
        api_key=key,
        base_url=base_url or os.getenv("EMBEDDING_BASE_URL", DEFAULT_BASE_URL),
        model=resolved_model,
        dim=resolved_dim,
    )


__all__ = [
    "ZhipuEmbeddingGateway",
    "build_embedding_gateway_from_env",
    "DEFAULT_BASE_URL",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_DIM",
]
