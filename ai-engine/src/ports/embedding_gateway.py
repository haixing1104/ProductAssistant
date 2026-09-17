"""EmbeddingGateway 端口：文本向量化（RAG / Milvus 语义召回的输入侧能力）。

只定义接口，不引入具体依赖；真实实现见 adapters/zhipu_embedding.py（智谱 embedding-3），
未配置时由调用方回退到 mock/关闭 RAG（等价空上下文）。
"""
from abc import ABC, abstractmethod

class EmbeddingGateway(ABC):
    """把一批文本编码为向量；维度须与 Milvus 集合 dim 对齐（见 EMBEDDING_DIM）。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码为向量。

        参数:
            texts: 待编码文本列表；实现方可按供应商批量上限自行分批。
        返回:
            与入参**等长且顺序一一对应**的向量列表（第 i 个向量对应 texts[i]）。
        异常:
            Exception: 网络 / 鉴权 / 协议错误由实现方抛自身异常类型；调用方按「RAG 关闭」降级。
        注意:
            向量维度必须与 Milvus 集合 dim 对齐（见 EMBEDDING_DIM / MILVUS_DIM），
            否则写入或检索会失败。
        """