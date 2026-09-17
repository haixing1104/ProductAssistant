"""条件边：决定 evaluate 之后与 HITL 之后的流转方向（纯函数）。"""

from __future__ import annotations

from ..state import ListingState, ensure_state

# 高价商品人工审批阈值：售价 > 500（元）时评估通过也不得自动上架，须人工放行
HIGH_VALUE_APPROVAL_THRESHOLD = 500

def product_price(raw_product_info: dict | None) -> float:
    """从商品素材里取售价（防御缺失/非法值，一律按 0 处理）。

    参数:
        raw_product_info: 商品素材 dict（读 base_price）；None/缺字段/非数值均按 0 处理。
    返回:
        售价 float；无法解析时返回 0.0（不抛异常，保证条件边永不因脏数据中断）。
    """
    try:
        return float((raw_product_info or {}).get("base_price", 0) or 0)
    except (TypeError, ValueError):
        return 0.0

def is_high_value_product(raw_product_info: dict | None) -> bool:
    """高价商品：售价严格大于阈值（=500 不触发人工审批）。

    参数:
        raw_product_info: 商品素材 dict。
    返回:
        True 表示售价 > HIGH_VALUE_APPROVAL_THRESHOLD，须人工放行；否则 False。
    """
    return product_price(raw_product_info) > HIGH_VALUE_APPROVAL_THRESHOLD

def should_retry_or_human(state: ListingState) -> str:
    """评估分流：决定 evaluate 之后走哪条边。

    分支口径:
        低价商品评估通过 → 路由键 "persist"（→ image_then_save → save_content 落库）；
        高价商品（>500）即使内容评估通过，也走 "human"（上架前须人工放行）；
        未过且次数未达上限 → "retry"（Reflection 重写）；
        未过且次数已耗尽 → "human"（HITL）。

    参数:
        state: 当前图状态；读 evaluation_result / evaluation_attempts / max_retries / raw_product_info。
    返回:
        路由键字符串："retry" / "persist" / "human"（映射关系见 graph/workflow.py）。
    """
    state = ensure_state(state)
    # 获取评估结果
    result = state.evaluation_result
    if result is not None and result.passed:
        if is_high_value_product(state.raw_product_info):
            return "human"
        return "persist" # save_content
    if state.evaluation_attempts < state.max_retries:
        return "retry"
    return "human"

def human_decision_route(state: ListingState) -> str:
    """HITL 恢复后的分流：approved→落库；rejected→驳回终态。

    参数:
        state: 当前图状态；读 approval（由 node_hitl 在 resume 后写入）。
    返回:
        路由键字符串："persist"（→ save_content 落库）或 "reject_end"（→ reject_end）。
    注意:
        返回的是「路由键」而非节点名："persist" 对应节点 save_content，
        与 evaluate 出口的 "persist"（对应 image_then_save）**不是同一个节点**。
    """
    state = ensure_state(state)
    return "persist" if state.approval == "approved" else "reject_end"

