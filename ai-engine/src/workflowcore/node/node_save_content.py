"""成功终态节点：经 ContentStore/EventBus 端口做“图结果→落库”边界动作。
节点本身不直连 DB，只调用注入的端口实现。"""

from __future__ import annotations

from ...ports import ContentRecord, ContentStore, EventBus, RAGStore
from ..state import ListingState, ensure_state


def _blocks_text(blocks: list[dict]) -> str:
    """从 content_blocks 抽取纯文本（无 generated_content 时的兜底）。

    参数:
        blocks: content_data.blocks 列表（仅取 type=="text" 的 text 字段）。
    返回:
        以换行拼接的纯文本；无文本 block 返回空串。
    """
    parts = [str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)

def save_content_node(
    state: ListingState,
    content_store: ContentStore | None = None,
    event_bus: EventBus | None = None,
    rag_store: RAGStore | None = None,
) -> dict:
    """把通过的生成内容写入 product_contents 并发布 done 终态事件。

    文案增量（content.chunk）由 generate 节点在流式生成时逐块发布（SSE 打字机输入源），
    本节点只负责落库与终态通知，避免内容被二次整段重放（去重回归）。

    rag_store：落库后 best-effort 把定稿文案写入向量库，供后续生成阶段 Few-Shot 召回。

    参数:
        state: 当前图状态；读 org_id / product_id / thread_id / content_blocks /
            generated_content / evaluation_result.passed / approval。
        content_store: 内容落库端口；None 时跳过落库（骨架/单测路径），仍会置终态。
        event_bus: 事件总线；None 时不发终态事件。
        rag_store: 向量库端口；None 时跳过写向量（RAG 可选增强）。
    返回:
        增量 dict：{"status": "succeeded"}。
    注意:
        · is_approved 取「评估通过 或 approval=="approved"」（HITL 放行的高价商品也记为已批准）；
        · 文案增量（content.chunk）由 node_generate 流式发布，本节点不重放内容，只落库+发终态；
        · RAG 写入失败被吞掉（best-effort），不影响落库与终态。
    """
    state = ensure_state(state)
    org_id, product_id, thread_id = state.org_id, state.product_id, state.thread_id
    passed = bool(state.evaluation_result is not None and state.evaluation_result.passed)
    approved = passed or state.approval == "approved"

    content_data = (
        {"blocks": state.content_blocks}
        if state.content_blocks
        else {"blocks": [{"type": "text", "text": state.generated_content or ""}]}
    )
    record = ContentRecord(
        org_id=org_id,
        product_id=product_id,
        thread_id=thread_id,
        content_data=content_data,
        version=1,
        is_approved=approved,
    )
    if content_store is not None:
        content_store.save(record)

    if rag_store is not None:
        # best-effort：定稿文案入向量库供后续 Few-Shot 召回；doc_id=thread_id 保证同一次生成幂等覆盖。
        try:
            rag_store.upsert(
                org_id=org_id,
                product_id=product_id,
                thread_id=thread_id,
                doc_id=thread_id,
                text=state.generated_content or _blocks_text(state.content_blocks),
            )
        except Exception:  # noqa: BLE001 RAG 为可选增强，写失败不阻断落库
            pass

    if event_bus is not None:
        # 终态事件发布
        event_bus.publish(f"evt:{thread_id}", {"type": "done", "data": {"product_id": product_id}})
    return {"status": "succeeded"}
