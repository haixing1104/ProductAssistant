"""HITL 节点：interrupt() 暂停图执行，等待 backend-api 审批后以 Command(resume=...) 恢复。
中断前副作用必须幂等；本节点不做任何通知，通知走 outbox（P4+）。"""

from __future__ import annotations

from langgraph.types import interrupt

from ..state import ListingState, ensure_state
from .node_conditions import is_high_value_product

def await_human_input(state: ListingState) -> dict:
    """暂停并请求人工审批（区分原因：高价商品需人工放行 / 内容质量多次未达标）。

    参数:
        state: 当前图状态；读 raw_product_info / product_id / thread_id / generated_content。
    返回:
        增量 dict：{"human_feedback": <原始 decision>, "approval": "approved"|"rejected"}。
    注意:
        · 首次执行会在此 interrupt 挂起，函数体不再往下走；resume 后会**重新执行本函数**，
          此时 interrupt() 直接返回 Command(resume=...) 传入的 decision；
        · 因此本节点在 interrupt 之前不得有不可重放的副作用；
        · 挂起依赖 checkpointer 持久化（生产见 graph.new_pg_checkpointer）。
    """

    state = ensure_state(state)
    high_value = is_high_value_product(state.raw_product_info)
    reason = "high_value" if high_value else "quality_exhausted"
    question = (
        "商品售价超过 ¥500（高价商品），需人工审批放行后方可上架。"
        if high_value
        else "内容质量多次未达标，请人工审批：批准放行或驳回。"
    )
    # 第一次调用时：传入interrupt的payload, 并进入挂起状态，等待Human介入
    # Command(resume)后，会重复进入该函数，此时decision=Command(resume=...)的值
    decision = interrupt(
        {
            "kind": "hitl.approval",
            "reason": reason,
            "product_id": state.product_id,
            "thread_id": state.thread_id,
            "content_preview": (state.generated_content or "")[:200],
            "question": question,
        }
    )

    if not isinstance(decision, dict):
        decision = {"approved": bool(decision), "feedback": ""}

    approved = bool(decision.get("approved", False))

    # Langgraph自动增量更新到ListState的human_feedback和approval字段
    return {"human_feedback": decision, "approval": "approved" if approved else "rejected"}