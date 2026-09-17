"""RAG 召回节点：经 RAGStore 端口取 Few-Shot 上下文并写入 rag_context。
    只要节点函数返回dict, Langgraph就会自动写入ListingState的rag_context字段进行增量更新
"""

from __future__ import annotations

from ...ports import RAGStore
from ..state import ListingState, ensure_state


def rag_retrieve_node(state: ListingState, rag: RAGStore | None = None) -> dict:
    """召回历史高转化文案作为 Few-Shot 上下文，写入 rag_context。

    参数:
        state: 当前图状态；读 org_id / product_id / raw_product_info.title 作为检索条件。
        rag: RAG 端口；None 时直接返回空上下文（骨架/未配置 Milvus 均可独立跑通）。
    返回:
        增量 dict：{"rag_context": ...}（多片段以换行拼接；失败/无命中为空串）。
    异常:
        Exception: rag.retrieve() 的异常由实现方抛出，本节点不做捕获
            （上游 worker 会统一终态化 failed，避免静默降级掩盖向量库故障）。
    """
    if rag is None:
        return {"rag_context": ""}
    state = ensure_state(state)
    # 商品基础资料
    info = state.raw_product_info or {}

    # 按商品语义召回历史高转化文案的topk个片段，作为生成阶段
    snippets = rag.retrieve(
        org_id=state.org_id,
        product_id=state.product_id,
        query=str(info.get("title") or ""),
        top_k=3,
    )
    # RAG 召回的历史高转化文案片段存入ListState.rag_context里（Langgraph会自动增量更新到ListingState的rag_context字段）
    return {"rag_context": "\n".join(snippets)}


