"""outbox 写入：与待审单**同事务**落库（Transactional Outbox 的写侧）。

为什么必须同事务:
    若「建审批单」与「写通知」分两次提交，中间崩溃就会出现「有待办但永远没人收到通知」——
    审批卡点静默停摆。同事务后二者要么都在、要么都不在。

落库粒度:
    每个渠道一行（``channel`` 是行属性），投递器按行独立重试：钉钉失败不会拖累其它渠道，
    渠道级故障可单独观测（``retry_count`` / ``status``）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import Settings
from ...models.orm import HitlApproval, NotificationOutbox, Product
from .message import build_approval_message

__all__ = ["enqueue_pending_notifications"]


async def enqueue_pending_notifications(
    session: AsyncSession,
    *,
    org_id: str,
    approval: HitlApproval,
    product: Product,
    settings: Settings,
    channels: tuple[str, ...],
) -> int:
    """为一条待审单写入各渠道的待投递记录。

    参数:
        session: 与建单同事务的会话（**不 commit**：事务边界由调用方掌握）。
        org_id: 组织 ID。
        approval: 待审单（读 id/thread_id/content_snapshot）。
        product: 商品（读 title/sku_code）。
        settings: 应用配置（深链基点等）。
        channels: 渠道名（来自 ``resolver.resolve_channels``）。
    返回:
        写入的行数（= ``len(channels)``）。
    """
    if not channels:
        return 0
    payload = build_approval_message(product=product, approval=approval, settings=settings)
    for channel in channels:
        session.add(
            NotificationOutbox(
                org_id=approval.org_id,
                channel=channel,
                payload=payload,
                status="pending",
                retry_count=0,
            )
        )
    await session.flush()
    return len(channels)
