"""组织（租户）路由：平台超管「组织选择器」的数据源。

为什么需要这个端点:
    超管的跨租户切换靠 ``X-Org-Id`` 头（契约见 ``core/deps``），前端必须有一份
    **权威**的组织列表可选；此前 ``organizations`` 只在 auth 内部被读，没有任何对外读取面。

权限口径（避免「列组织」变成新的越权面）:
    · 仅 ``admin`` 可访问（与成员管理 / 运维面同档）；
    · **超管** → 返回全部组织（含 ``status``，前端据此禁用已停用项）；
    · **普通 admin** → 只返回自己那一个组织（列表恒为 1 条）—— 人工构造请求也拿不到别家组织名。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..models.orm import Organization
from .common import ok

__all__ = ["router"]

router = APIRouter(prefix="/orgs", tags=["orgs"])


@router.get("")
async def list_orgs(
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """列出可选组织（超管 = 全部；普通 admin = 仅自己所属组织）。

    返回:
        ``[{"id", "name", "status"}]``，按创建时间升序（顺序稳定，便于前端定位与缓存）。
    """
    stmt = select(Organization).order_by(Organization.created_at)
    if not user.is_superuser:
        stmt = stmt.where(Organization.id == uuid.UUID(user.org_id))
    rows = (await session.execute(stmt)).scalars().all()
    return ok([{"id": str(row.id), "name": row.name, "status": row.status} for row in rows])
