"""HITL 驳回终态节点：仅更新状态，不写 product_contents。"""

from __future__ import annotations

from ..state import ListingState, ensure_state


def reject_end_node(state: ListingState) -> dict:
    """HITL 驳回终态：仅置终态字段，不落 product_contents、不写向量库。

    参数:
        state: 当前图状态；读 approval（为空时归一为 "rejected"）。
    返回:
        增量 dict：{"status": "rejected", "approval": ...}。
    注意:
        驳回路径无任何存储副作用；终态事件由 worker 侧按 status 发布（rejected）。
    """
    state = ensure_state(state)
    return {"status": "rejected", "approval": state.approval or "rejected"}
