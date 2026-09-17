"""仓储层基类：**强制**租户过滤的单点。

为什么要有这个基类:
    「查询一律带 ``org_id``」是本仓库的多租户红线（越权 = 数据泄露）。
    如果每个仓储各写各的 where，早晚会漏一处。这里把 ``org_id`` 收进构造参数，
    并提供统一的 ``_scoped(stmt)``，让「忘记加租户过滤」变成一件**别扭**的事。

约定:
    · 仓储只做数据访问，不做业务判断（业务在 services/）；
    · 仓储**不 commit**：事务边界由服务层显式控制（一次业务操作 = 一次 commit）。
"""

from __future__ import annotations

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["OrgScopedRepo"]


class OrgScopedRepo:
    """带租户上下文的仓储基类。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化。

        参数:
            session: 请求级 DB 会话。
            org_id: 当前租户（来自 JWT claims，**不是**请求参数）。
        """
        self.session = session
        self.org_id = org_id

    def _scoped(self, stmt: Select, model) -> Select:
        """给查询语句附加租户过滤（所有查询都应经过这里）。

        参数:
            stmt: 基础 select 语句。
            model: 目标 ORM 模型（取其 ``org_id`` 列）。
        返回:
            追加了 ``WHERE model.org_id = :org_id`` 的语句。
        """
        return stmt.where(model.org_id == self.org_id)
