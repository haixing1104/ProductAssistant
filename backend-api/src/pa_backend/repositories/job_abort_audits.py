"""手工终止任务的审计仓储（``job_abort_audits``，只增不改）。

权限红线（``database/sql/0005_job_abort_audit.sql``）:
    ``role_pa_backend`` 对 ``job_abort_audits`` 只有 **SELECT/INSERT**（UPDATE/DELETE 已 REVOKE）。
    因此本仓储**只提供 insert 与查询** —— 审计可追溯的前提就是「改不掉、删不掉」
    （与 ``repositories/audits.py`` 的 ``AuditRepo`` 同一形态）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import JobAbortAudit

__all__ = ["JobAbortAuditRepo"]


class JobAbortAuditRepo:
    """运维手工终止任务的审计仓储。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。

        参数:
            session: DB 会话。
            org_id: 当前租户（写审计时落列，便于按租户回查）。
        """
        self.session = session
        self.org_id = org_id

    async def record(
        self,
        *,
        job_id: uuid.UUID,
        thread_id: uuid.UUID,
        product_id: uuid.UUID,
        actor_user_id: str,
        reason: str,
    ) -> JobAbortAudit:
        """写入一条终止审计（调用方负责 commit，与任务收口同事务）。"""
        audit = JobAbortAudit(
            org_id=uuid.UUID(self.org_id),
            job_id=job_id,
            thread_id=thread_id,
            product_id=product_id,
            actor_user_id=uuid.UUID(actor_user_id),
            reason=reason,
        )
        self.session.add(audit)
        await self.session.flush()
        return audit

    async def list_for_product(self, product_id: uuid.UUID) -> list[JobAbortAudit]:
        """列出某商品的终止审计（新→旧）。"""
        stmt = (
            select(JobAbortAudit)
            .where(JobAbortAudit.org_id == self.org_id, JobAbortAudit.product_id == product_id)
            .order_by(JobAbortAudit.aborted_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
