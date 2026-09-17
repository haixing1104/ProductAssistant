"""审计与只读展示仓储：``delete_audits``（只增不改）与 ``schema_pa_ai`` 两张只读表。

权限红线（``database/sql/0004_delete_audit.sql``）:
    ``role_pa_backend`` 对 ``delete_audits`` 只有 **SELECT/INSERT**（UPDATE/DELETE 被 REVOKE）。
    因此本仓储**只提供 insert 与查询** —— 审计表不能改也不能删，否则等于没有审计
    （红线由 ``tests/test_roles_red_lines.py`` 断言）。

AI 域只读（``0002_roles_grants.sql``）:
    ``product_contents`` / ``evaluation_logs`` 对 backend 仅 SELECT；写归 ai-engine。
    本仓储只读，且在方法名上明示 ``readonly`` 语义，避免后来者顺手加写方法。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import DeleteAudit, EvaluationLog, ProductContent

__all__ = ["AuditRepo", "ContentsRepo", "EvaluationsRepo"]


class AuditRepo:
    """彻底删除审计（只增不改，见模块 docstring）。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。

        参数:
            session: DB 会话。
            org_id: 当前租户（写审计时写入该列，便于按租户查询）。
        """
        self.session = session
        self.org_id = org_id

    async def record(
        self, *, product_id: uuid.UUID, sku_code: str, title: str, actor_user_id: str, reason: str
    ) -> DeleteAudit:
        """写入一条删除审计（调用方负责 commit，与删除动作同事务）。"""
        audit = DeleteAudit(
            org_id=self.org_id,
            product_id=product_id,
            sku_code=sku_code,
            title=title,
            actor_user_id=uuid.UUID(actor_user_id),
            reason=reason,
        )
        self.session.add(audit)
        await self.session.flush()
        return audit

    async def list_for_product(self, product_id: uuid.UUID) -> list[DeleteAudit]:
        """列出某商品的删除审计（新→旧）。"""
        stmt = (
            select(DeleteAudit)
            .where(DeleteAudit.org_id == self.org_id, DeleteAudit.product_id == product_id)
            .order_by(DeleteAudit.deleted_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ContentsRepo:
    """``schema_pa_ai.product_contents`` 只读（版本切换与图文展示）。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。"""
        self.session = session
        self.org_id = org_id

    async def list_versions(self, product_id: uuid.UUID) -> list[ProductContent]:
        """列出某商品全部内容版本（新→旧，便于前端默认展示最新）。"""
        stmt = (
            select(ProductContent)
            .where(ProductContent.org_id == self.org_id, ProductContent.product_id == product_id)
            .order_by(ProductContent.version.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest(self, product_id: uuid.UUID) -> ProductContent | None:
        """取最新版本（无版本返回 None —— 新商品冷启动属正常，不报错）。"""
        stmt = (
            select(ProductContent)
            .where(ProductContent.org_id == self.org_id, ProductContent.product_id == product_id)
            .order_by(ProductContent.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class EvaluationsRepo:
    """``schema_pa_ai.evaluation_logs`` 只读（AI 思考轨迹面板的数据源）。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。"""
        self.session = session
        self.org_id = org_id

    async def list_logs(self, product_id: uuid.UUID, *, limit: int = 50) -> list[EvaluationLog]:
        """列出某商品的评估日志（新→旧）。

        参数:
            product_id: 商品 ID。
            limit: 上限（默认 50；Trace 面板按次数分页展示，不需要全量拉取）。
        """
        stmt = (
            select(EvaluationLog)
            .where(EvaluationLog.org_id == self.org_id, EvaluationLog.product_id == product_id)
            .order_by(EvaluationLog.created_at.desc())
            .limit(max(1, limit))
        )
        return list((await self.session.execute(stmt)).scalars().all())
