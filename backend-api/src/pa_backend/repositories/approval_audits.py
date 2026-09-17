"""审批审计仓储（``approval_redrive_audits`` / ``approval_overrides``，只增不改）。

权限红线（``database/sql/0006_approval_audits.sql``）:
    ``role_pa_backend`` 对两张审计表只有 **SELECT/INSERT**（UPDATE/DELETE 已 REVOKE），
    因此本仓储只提供 insert 与查询 —— 审计可追溯的前提就是「改不掉、删不掉」
    （与 ``repositories/job_abort_audits.py`` 同一形态）。

为什么需要（2026-09 实测）:
    · 补投按钮此前没有任何痕迹，分不清「引擎已收到（无需补投）」与「投出去了没消费」；
    · 合规命中后人工放行没有留痕，事后无法回答"谁在知情下放行了哪条命中点"。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import ApprovalOverride, ApprovalRedriveAudit

__all__ = [
    "ApprovalAuditRepo",
    "overrides_by_approval",
    "redrive_summary_by_approval",
]


class ApprovalAuditRepo:
    """审批域审计读写（只增不改）。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。

        参数:
            session: DB 会话（调用方负责 commit，与审批动作同事务）。
            org_id: 当前租户。
        """
        self.session = session
        self.org_id = org_id

    async def record_redrive(
        self,
        *,
        approval_id: uuid.UUID,
        thread_id: uuid.UUID,
        outcome: str,
        actor_user_id: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRedriveAudit:
        """写入一条补投审计（``outcome`` 见 ``approval_service.redrive``）。"""
        audit = ApprovalRedriveAudit(
            org_id=uuid.UUID(self.org_id),
            approval_id=approval_id,
            thread_id=thread_id,
            actor_user_id=uuid.UUID(actor_user_id) if actor_user_id else None,
            outcome=outcome,
            reason=reason,
        )
        self.session.add(audit)
        await self.session.flush()
        return audit

    async def record_override(
        self,
        *,
        approval_id: uuid.UUID,
        thread_id: uuid.UUID,
        actor_user_id: str,
        violations: list,
        reason: str,
    ) -> ApprovalOverride:
        """写入一条「人工放行带命中点」审计（与审批定案同事务）。"""
        override = ApprovalOverride(
            org_id=uuid.UUID(self.org_id),
            approval_id=approval_id,
            thread_id=thread_id,
            actor_user_id=uuid.UUID(actor_user_id),
            violation_count=len(violations),
            violations=list(violations),
            reason=reason,
        )
        self.session.add(override)
        await self.session.flush()
        return override


async def redrive_summary_by_approval(
    session: AsyncSession, *, org_id: str, approval_ids: list[uuid.UUID]
) -> dict[str, dict]:
    """批量取补投摘要：``{approval_id: {"count", "last_at", "last_outcome"}}``（无记录则不出现）。

    参数:
        session: DB 会话。
        org_id: 租户（多租户隔离的第一过滤条件）。
        approval_ids: 待查审批单 ID（空列表直接返回 {}，不发起查询）。
    返回:
        approval_id（str）→ 摘要 dict；**只在有审计行时出现该键**（前端据"有无"决定是否展示）。
    """
    if not approval_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ApprovalRedriveAudit.approval_id,
                func.count().label("count"),
                func.max(ApprovalRedriveAudit.redriven_at).label("last_at"),
            )
            .where(
                ApprovalRedriveAudit.org_id == uuid.UUID(org_id),
                ApprovalRedriveAudit.approval_id.in_(approval_ids),
            )
            .group_by(ApprovalRedriveAudit.approval_id)
        )
    ).all()
    latest = (
        await session.execute(
            select(
                ApprovalRedriveAudit.approval_id,
                ApprovalRedriveAudit.outcome,
                ApprovalRedriveAudit.redriven_at,
            )
            .where(
                ApprovalRedriveAudit.org_id == uuid.UUID(org_id),
                ApprovalRedriveAudit.approval_id.in_(approval_ids),
            )
            .order_by(ApprovalRedriveAudit.redriven_at.desc())
        )
    ).all()
    last_outcome: dict[str, tuple[str, object]] = {}
    for approval_id, outcome, at in latest:
        last_outcome.setdefault(str(approval_id), (outcome, at))
    out: dict[str, dict] = {}
    for approval_id, count, last_at in rows:
        key = str(approval_id)
        outcome, at = last_outcome.get(key, (None, None))
        out[key] = {
            "count": int(count),
            "last_at": (at or last_at).isoformat() if (at or last_at) else None,
            "last_outcome": outcome,
        }
    return out


async def overrides_by_approval(
    session: AsyncSession, *, org_id: str, approval_ids: list[uuid.UUID]
) -> dict[str, dict]:
    """批量取「人工放行」审计：``{approval_id: {"reason", "violation_count", "at"}}``。"""
    if not approval_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ApprovalOverride.approval_id,
                ApprovalOverride.reason,
                ApprovalOverride.violation_count,
                ApprovalOverride.created_at,
            )
            .where(
                ApprovalOverride.org_id == uuid.UUID(org_id),
                ApprovalOverride.approval_id.in_(approval_ids),
            )
            .order_by(ApprovalOverride.created_at.desc())
        )
    ).all()
    out: dict[str, dict] = {}
    for approval_id, reason, count, at in rows:
        out.setdefault(
            str(approval_id),
            {
                "reason": reason,
                "violation_count": int(count),
                "at": at.isoformat() if at else None,
            },
        )
    return out
