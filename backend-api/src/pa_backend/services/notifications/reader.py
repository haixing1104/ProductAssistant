"""``notification_outbox`` 只读视图：把「待审通知到底发出去了没有」变成接口可查的事实。

为什么运维/审批人需要看见它:
    通知投递是**异步、可失败、可重试**的（``deliverer`` 指数退避 + ``max_attempts`` 后置 ``dlq``）。
    如果接口不暴露状态，审批人只能凭猜：单子建了但没人收到通知时，界面上看不出任何区别 ——
    而「通知没发出去」恰恰是审批卡住最常见的原因。

字段口径（与 PP 的一处**关键差异**）:
    PP 用 ``payload->>'kind' == 'hitl.pending'`` 过滤；**PA 的载荷里没有 ``kind``**
    （PA 的 outbox 只写「待审通知」这一类，写入点在 ``writer.enqueue_pending_notifications``）。
    因此这里只按 ``org_id`` + ``payload->>'approval_id'`` 过滤 —— 照抄 PP 的过滤条件会
    永远查不到任何一行（静默返回空列表）。

失败原因:
    ``notification_outbox`` **没有 error 列**，故失败原因由 ``deliverer`` 写入
    ``payload['last_error']``（诊断字段，不参与消息渲染）。读取时容忍缺失。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.orm import NotificationOutbox

__all__ = ["notifications_by_approval"]

#: 单条失败原因的最大保留长度（避免把整段 SMTP 交互写进 jsonb）
MAX_ERROR_CHARS = 500


async def notifications_by_approval(
    session: AsyncSession, *, org_id: str, approval_ids: list[uuid.UUID]
) -> dict[str, list[dict[str, Any]]]:
    """批量取「某租户、某几审批单」的外发通知投递状态。

    参数:
        session: DB 会话。
        org_id: 租户（审批行已按租户过滤，这里是第二道闸）。
        approval_ids: 审批单 ID 列表。
    返回:
        ``{approval_id: [{channel, status, retry_count, next_retry_at, provider_msg_id, error,
        created_at, updated_at}, …]}``；无记录的单子不出现在 dict 里（调用方回退 ``[]``）。
    注意:
        一次 ``in`` 查询取完，避免审批列表 N+1；按 ``channel`` 稳定排序，前端展示顺序可预期。
    """
    ids = [str(item) for item in approval_ids]
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.org_id == org_id,
                    NotificationOutbox.payload["approval_id"].astext.in_(ids),
                )
                .order_by(NotificationOutbox.channel.asc(), NotificationOutbox.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = row.payload or {}
        approval_id = str(payload.get("approval_id") or "")
        if not approval_id:
            continue  # 载荷缺 approval_id（历史/异常行）：不硬塞进某个单子
        out.setdefault(approval_id, []).append(
            {
                "channel": row.channel,
                "status": row.status,
                "retry_count": row.retry_count,
                "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                "provider_msg_id": row.provider_msg_id,
                "error": payload.get("last_error"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return out
