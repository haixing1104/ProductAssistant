"""Milvus RAG 真实 adapter：商品文案语义召回的读写边界（RAGStore 端口实现）。

职责边界:
    仅承载历史高转化文案向量（Few-Shot 召回），不承载合规判定（合规由规则引擎负责）。

多租户:
    检索/删除一律带 org_id 过滤，跨租户数据永不互相可见。

召回口径（两段式「同商品优先 → 同租户兜底」）:
    ① 同商品：filter = org_id == X and product_id == Y   → 高相关样例优先（品类话术强相关）
    ② 同租户：filter = org_id == X and product_id != Y   → 冷启动兜底（新商品无历史时仍可召回）
    两段结果按 ① + ② 顺序拼接后截断到 top_k；详细说明见 retrieve()。

降级策略:
    运行期任何异常一律吞掉（retrieve 返回 []、upsert/delete 静默），绝不阻断生成/落库主链路。

命名与字段:
    集合名固定为 pa_listing_vec（常量 LISTING_VEC_COLLECTION），字段定义见
    database/milvus/init_collections.py；
    ⚠ 与 database/milvus/init_collections.py::LISTING_VEC_COLLECTION 必须保持一致。

环境变量:
    - MILVUS_URI：Milvus 服务地址；未配置则不构造实例（RAG 关闭）。
    - MILVUS_DIM：向量维度，默认 1024（须与 EMBEDDING_DIM / 集合 dim 对齐）。
"""

from __future__ import annotations

import logging
import os

from ..ports import EmbeddingGateway, RAGStore
from .zhipu_embedding import build_embedding_gateway_from_env

logger = logging.getLogger(__name__)

# 向量维度默认值：须与 EMBEDDING_DIM / 集合 dim 对齐（见 database/milvus/init_collections.py）。
DEFAULT_DIM = 1024

# text 字段在集合中是 VARCHAR(8192)，这里截断到 8000 留余量（多字节字符按字节计长时的安全边际）。
TEXT_MAX_LEN = 8000

# 集合名：固定字面量，不带环境前缀。
# ⚠ 与 database/milvus/init_collections.py::LISTING_VEC_COLLECTION 必须保持一致，
#   否则会「写入 A 集合、检索 B 集合」，表现为 retrieve() 恒为空（静默降级），难以排查。
LISTING_VEC_COLLECTION = "pa_listing_vec"


def _hit_text(hit) -> str | None:
    """从 pymilvus 检索命中项提取 text 字段。

    兼容两种返回形态（pymilvus 版本 / 调用方式差异）：
        - MilvusClient.search 的 dict 命中：{"id": ..., "distance": ..., "entity": {"text": ...}}
        - 旧式 Hit 对象：hit.entity.get("text")
    参数:
        hit: 单条检索命中项。
    返回:
        text 明文；取不到时返回 None（调用方跳过该条），不抛异常。
    """
    if isinstance(hit, dict):
        entity = hit.get("entity")
    else:
        entity = getattr(hit, "entity", None)
    if entity is None:
        return None
    if isinstance(entity, dict):
        return entity.get("text")
    try:  # pymilvus Hit.entity.get("text")
        return entity.get("text")
    except Exception:  # noqa: BLE001
        return None


class MilvusRAGStore(RAGStore):
    """Milvus 实现：query 先经 EmbeddingGateway 编码，再以 org_id 过滤做向量检索。

    召回口径为两段式「同商品优先 → 同租户兜底」，详见 retrieve()。
    """

    def __init__(self, *, client, collection: str, embed_gateway: EmbeddingGateway, dim: int = DEFAULT_DIM) -> None:
        """构造实例（依赖装配由 build_milvus_rag_store_from_env 负责）。

        参数:
            client: pymilvus.MilvusClient；单测可注入假客户端。
            collection: 目标集合名，应为 LISTING_VEC_COLLECTION。
            embed_gateway: 文本向量化网关（智谱 embedding-3）。
            dim: 期望向量维度；仅作记录便于排障，须与集合 dim 对齐（不参与检索）。
        """
        self._client = client
        self._collection = collection
        self._embed = embed_gateway
        self._dim = int(dim)

    def _search(self, vectors: list[list[float]], filter_expr: str, limit: int) -> list[str]:
        """执行一次向量检索并按相似度降序返回 text 列表（不吞异常，由调用方统一降级）。

        参数:
            vectors: 查询向量（批量列表，本 adapter 每次只传 1 条）。
            filter_expr: Milvus 标量过滤表达式；多租户约束要求必须包含 org_id 条件。
            limit: 本次最多返回条数（调用方保证 >= 1）。
        返回:
            按相似度降序排列的 text 列表；无命中返回 []。
        """
        res = self._client.search(
            collection_name=self._collection,
            data=vectors,                 # 批量查询：返回结构为 [nq][limit]
            filter=filter_expr,           # 标量过滤：多租户 / 商品维度都在这里约束
            limit=limit,
            output_fields=["text"],       # 只回取明文，不回传向量（省带宽）
        )
        snippets: list[str] = []
        for hits in res or []:            # nq 维度（每个查询向量一组命中）
            for hit in hits:              # 单个查询的 top-N
                text = _hit_text(hit)
                if text:
                    snippets.append(text)
        return snippets

    def retrieve(self, *, org_id: str, product_id: str, query: str, top_k: int = 3) -> list[str]:
        """两段式语义召回历史高转化文案，作为生成阶段的 Few-Shot 上下文。

        流程:
            1. query 明文 → embedding 模型编码为查询向量；
            2. 第一段「同商品」：org_id == X and product_id == Y，取最多 top_k 条高相关样例；
            3. 若第一段不足 top_k，第二段「同租户」兜底：
               org_id == X and product_id != Y，补足剩余条数（新商品无历史时不至于召回为空）；
            4. 两段结果按 ① + ② 拼接（同商品样例在前）并截断到 top_k。

        参数:
            org_id: 租户 ID；多租户隔离的硬约束，两段过滤都必须带。
            product_id: 商品 ID；第一段的优先过滤维度，冷启动时自动降级到第二段。
            query: 查询明文（通常是本次商品的标题 / 卖点描述）。
            top_k: 期望返回条数上限（至少 1，小于 1 时按 1 处理）。
        返回:
            文案片段列表，长度 <= top_k；同商品样例在前，同租户兜底样例在后。
            任何失败（embedding 或检索异常）都返回 []，等价「无 RAG 上下文」，不阻断生成。
        """
        query = (query or "").strip()
        if not query:
            return []
        k = max(1, int(top_k))  # 下限保护：避免 limit=0 导致 Milvus 参数非法
        try:
            vectors = self._embed.embed([query])
            if not vectors:
                return []  # embedding 不可用（未配置 / 调用失败）：静默降级为空上下文
            # 第一段：同商品高相关样例。爆款话术与品类强相关，优先喂给模型
            # org_id / product_id 均为系统内部 UUID（非用户输入），不存在注入面
            same = self._search(
                vectors,
                f'org_id == "{org_id}" and product_id == "{product_id}"',
                k,
            )
            if len(same) >= k:
                return same[:k]
            # 第二段：同租户兜底（排除本商品，与第一段条件互斥，故两段拼接天然无重复）
            rest = self._search(
                vectors,
                f'org_id == "{org_id}" and product_id != "{product_id}"',
                k - len(same),
            )
        except Exception as exc:  # noqa: BLE001 降级：召回失败不阻断生成
            logger.warning("Milvus RAG 召回失败（降级为空上下文）: %r", exc)
            return []
        return (same + rest)[:k]

    def upsert(self, *, org_id: str, product_id: str, thread_id: str, doc_id: str, text: str) -> None:
        """写入/覆盖一条文案向量（批准定稿后入库，供后续召回）。

        流程:
            text 明文 → embedding 模型编码为向量 → 明文与向量一起 upsert 进 Milvus。

        参数:
            org_id: 租户 ID（写入字段；检索时作为必带过滤条件）。
            product_id: 商品 ID（写入字段；用于「同商品优先」召回与按商品删除）。
            thread_id: 生成任务线程 ID（回溯锚点，关联 generation_jobs / product_contents）。
            doc_id: 主键 ID；同一 doc_id 重复写入即幂等覆盖，不会产生重复向量。
            text: 待入库文案明文；空串直接跳过，超长按 TEXT_MAX_LEN 截断。
        返回:
            None；任何失败只记 warning 不向上抛（写失败不阻断落库主链路）。
        """
        text = (text or "").strip()
        if not text:
            return
        # 向量与明文共用同一份截断文本，保证「检索到的向量」与「回传的 text」语义一致
        clipped = text[:TEXT_MAX_LEN]
        try:
            vectors = self._embed.embed([clipped])
            if not vectors:
                return  # embedding 不可用：跳过写入（等价 RAG 关闭）
            self._client.upsert(
                collection_name=self._collection,
                data=[
                    {
                        "id": doc_id,                 # 主键：同 id 覆盖，实现幂等
                        "org_id": org_id,             # 多租户隔离字段（检索必带）
                        "product_id": product_id,     # 商品维度（同商品优先 / 按商品删除）
                        "thread_id": thread_id,       # 回溯锚点
                        "text": clipped,              # Few-Shot 上下文原文
                        "embedding": vectors[0],      # 与 text 对应的向量
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 降级：写失败不阻断落库
            logger.warning("Milvus RAG 写入失败（忽略）: %r", exc)

    def delete_by_product(self, *, org_id: str, product_id: str) -> int:
        """删除某商品的全部向量（admin 彻底删除 / purge 联动清理）。

        参数:
            org_id: 租户 ID。
            product_id: 商品 ID。
        返回:
            实际删除条数；任何失败返回 0（best-effort，不阻断删除主流程）。
        """
        try:
            # org_id + product_id 双条件：避免跨租户误删同 ID 商品的数据
            res = self._client.delete(
                collection_name=self._collection,
                filter=f'org_id == "{org_id}" and product_id == "{product_id}"',
            )
        except Exception as exc:  # noqa: BLE001 降级：清理失败不影响主流程
            logger.warning("Milvus RAG 删除失败（忽略）: %r", exc)
            return 0
        # MilvusClient.delete 返回 {"delete_count": n}；兼容非 dict 返回（旧版本 / 假客户端）
        if isinstance(res, dict):
            return int(res.get("delete_count", 0) or 0)
        return 0


def build_milvus_rag_store_from_env(*, client=None, embed_gateway: EmbeddingGateway | None = None) -> MilvusRAGStore | None:
    """按环境变量构建 Milvus RAG Store；依赖缺失时返回 None（RAG 关闭，等价空上下文）。

    参数:
        client: 可注入的 Milvus 客户端（单测传假客户端，从而不依赖 pymilvus）。
            None 时惰性构造 pymilvus.MilvusClient。
        embed_gateway: 可注入的向量化网关；None 时按环境变量构建（未配置即 None）。
    返回:
        MilvusRAGStore 实例；None 表示 RAG 未启用（调用方按「无 RAG 上下文」处理，不报错）。
    说明:
        惰性 import pymilvus：未安装 / 连不上 Milvus 都只记 warning 并关闭 RAG，不影响主链路。
    """
    uri = os.getenv("MILVUS_URI")
    if not uri:
        return None  # 未配置 Milvus 地址：直接关闭 RAG
    gateway = embed_gateway if embed_gateway is not None else build_embedding_gateway_from_env()
    if gateway is None:
        return None  # 未配置 embedding（Key / 模型缺失或 provider=mock）：同样关闭 RAG
    if client is None:
        try:
            from pymilvus import MilvusClient  # 延迟导入：未安装不影响主链路
        except Exception as exc:  # noqa: BLE001
            logger.warning("pymilvus 不可用（RAG 关闭）: %r", exc)
            return None
        try:
            client = MilvusClient(uri=uri)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 初始化失败（RAG 关闭）: %r", exc)
            return None
    dim = int(os.getenv("MILVUS_DIM", str(DEFAULT_DIM)))
    return MilvusRAGStore(
        client=client,
        collection=LISTING_VEC_COLLECTION,
        embed_gateway=gateway,
        dim=dim,
    )


__all__ = [
    "MilvusRAGStore",
    "build_milvus_rag_store_from_env",
    "LISTING_VEC_COLLECTION",
    "DEFAULT_DIM",
    "TEXT_MAX_LEN",
]
