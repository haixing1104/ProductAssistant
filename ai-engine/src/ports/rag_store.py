"""RAGStore 端口：Milvus 语义召回的读写边界（只做 Few-Shot 召回，不承载合规判定）。

读：retrieve（生成阶段召回历史高转化文案作为 Few-Shot）；
写：upsert（批准文案入库，供后续召回）；
delete_by_product（admin 彻底删除联动清理）。
多租户：所有操作必须带 org_id 过滤。
"""

from abc import ABC, abstractmethod

class RAGStore(ABC):
    """RAG 向量库端口：Milvus 语义召回的读写边界（读=召回、写=入库、删=purge 联动）。

    实现见 adapters/milvus_rag_store.py；未配置时由上层注入 None（等价空 Few-Shot 上下文）。
    """

    @abstractmethod
    def retrieve(self, *, org_id: str, product_id: str, query: str, top_k: int = 3) -> list[str]:
        """按商品语义召回历史高转化文案片段，作为生成阶段 Few-Shot 示例。

        实现口径约定（见 adapters/milvus_rag_store.py::MilvusRAGStore.retrieve）：
            "同商品优先 → 同租户兜底"两段式 —— product_id 用于优先召回本商品样例，
            样例不足时用同租户其他商品的样例补足，避免新商品冷启动时召回恒为空；
            返回条数不超过 top_k，且所有样例均限定在同一 org_id 内（多租户隔离）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件，实现方必须强制附加）。
            product_id: 商品 ID（用于「同商品优先」加权/前插）。
            query: 检索文本，通常为商品标题等生成素材。
            top_k: 返回条数上限（默认 3）。
        返回:
            历史文案片段列表；不足或未启用时返回 []（调用方按空上下文继续生成，不报错）。
        异常:
            Exception: 向量库/Embedding 故障由实现方抛出；调用方应降级为空上下文（不阻断生成）。
        """

    @abstractmethod
    def upsert(self, *, org_id: str, product_id: str, thread_id: str, doc_id: str, text: str) -> None:
        """写入/覆盖一条文案向量（批准定稿内容入库；同 doc_id 幂等覆盖）。

        参数:
            org_id: 组织 ID（写入字段，检索时作为必带过滤条件）。
            product_id: 商品 ID（支撑「同商品优先」召回与按商品删除）。
            thread_id: 生成任务线程 ID（回溯锚点，关联 generation_jobs / product_contents）。
            doc_id: 主键 ID；同一 doc_id 重复写入即幂等覆盖，不产生重复向量。
            text: 待入库文案明文（通常是批准定稿的 generated_content）。
        返回:
            无返回值。
        异常:
            Exception: 向量库/Embedding 故障由实现方抛出；调用方按 best-effort 处理（不阻断落库）。
        """

    @abstractmethod
    def delete_by_product(self, *, org_id: str, product_id: str) -> int:
        """删除某商品全部向量（彻底删除联动），返回删除条数（best-effort，失败返回 0）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
        返回:
            实际删除的向量条数；失败或未启用返回 0（调用方不据此判定成败）。
        异常:
            Exception: 向量库故障由实现方抛出；purge 流程会捕获并记为 best-effort 失败。
        """