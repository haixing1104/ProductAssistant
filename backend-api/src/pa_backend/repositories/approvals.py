"""审批单的只读查询（供生成链路取「最近一次驳回意见」）。

为什么单独一个模块（而不是塞进 ``approval_service``）:
    ``product_service.trigger_generation`` 需要这条查询，而 ``approval_service`` 的构造会顺手
    建 Redis 投递客户端（对"只是读一条意见"来说太重）。这里是纯 DB 只读，无副作用。

背景（2026-09 实测缺口）:
    驳回意见此前只落 ``hitl_approvals.feedback``，**没有任何确定性路径**进入下一轮生成 ——
    唯一的消费者是 Agent 的 ``get_approval_history`` 工具，要靠模型自己决定去查。
    结果是"驳回 → 重新生成"实质只是"重跑一次"，审批人的意见大概率白写。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import HitlApproval

__all__ = ["latest_rejected_feedback"]


async def latest_rejected_feedback(
    session: AsyncSession, *, org_id: str, product_id: uuid.UUID
) -> str | None:
    """取该商品最近一次「驳回」的审批意见（新→旧，第一条非空 feedback）。

    参数:
        session: DB 会话（只读）。
        org_id: 租户（多租户隔离的第一过滤条件）。
        product_id: 商品 ID。
    返回:
        驳回意见文本；从未被驳回 / 意见为空时返回 None（调用方按"无额外指引"处理）。
    注意:
        只取 ``status='rejected'``：批准时的意见是对"已通过内容"的评价，不该当作改写指引。
    """
    feedback = (
        await session.execute(
            select(HitlApproval.feedback)
            .where(
                HitlApproval.org_id == uuid.UUID(org_id),
                HitlApproval.product_id == product_id,
                HitlApproval.status == "rejected",
                HitlApproval.feedback.is_not(None),
            )
            .order_by(HitlApproval.resolved_at.desc().nullslast(), HitlApproval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    text = (feedback or "").strip()
    return text or None
