"""用户（成员）仓储：本组织内的用户读写。

权限边界:
    本仓储只服务 admin 的成员管理（``/users``）与登录鉴权所需的按名查找。
    ``sys_users`` 对 ``role_pa_ai`` **零权限**（`0002_roles_grants.sql`）——
    所以 ai-engine 侧只能拿到 ``approver_id``，姓名要由本模块 join 后下发。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import SysUser
from .base import OrgScopedRepo

__all__ = ["UserRepo"]


class UserRepo(OrgScopedRepo):
    """本租户的用户仓储。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化（见 ``OrgScopedRepo``）。"""
        super().__init__(session, org_id)

    async def list_members(self, *, offset: int, limit: int) -> tuple[list[SysUser], int]:
        """分页列出本组织成员（按创建时间倒序）。

        参数:
            offset: 偏移量（>= 0）。
            limit: 每页条数（>= 1）。
        返回:
            ``(items, total)``；``total`` 用于响应头 ``X-Total-Count``。
        """
        base = self._scoped(select(SysUser), SysUser)
        total = (await self.session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (
            (
                await self.session.execute(
                    base.order_by(SysUser.created_at.desc()).offset(max(0, offset)).limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def get(self, user_id: uuid.UUID) -> SysUser | None:
        """按 id 取本组织成员（越权与不存在同返回 None）。"""
        stmt = self._scoped(select(SysUser), SysUser).where(SysUser.id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_username(self, username: str) -> SysUser | None:
        """按用户名取本组织成员（登录消歧后使用）。"""
        stmt = self._scoped(select(SysUser), SysUser).where(SysUser.username == username)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, *, username: str, hashed_password: str, role: str) -> SysUser:
        """新增成员（``(org_id, username)`` 冲突时抛 ``IntegrityError`` 由服务层转 409）。

        参数:
            username: 登录名（组织内唯一）。
            hashed_password: bcrypt 哈希（明文绝不入库）。
            role: ``reviewer`` / ``operator``。
        返回:
            新建的 ``SysUser``（已 flush，拿到 id）。
        """
        user = SysUser(org_id=self.org_id, username=username, hashed_password=hashed_password, role=role)
        self.session.add(user)
        await self.session.flush()
        return user


def is_username_conflict(error: IntegrityError) -> bool:
    """判断异常是否为「组织内用户名重复」（唯一约束冲突）。

    参数:
        error: SQLAlchemy 抛出的 ``IntegrityError``。
    返回:
        命中 ``uq_sys_users_org_username`` 时 True。
    注意:
        依赖约束名判断（而不是错误文案模糊匹配）：约束名是 ``0001_schema.sql`` 里显式命名的，
        改名字会同时改这里 —— 比字符串匹配稳定。
    """
    return "uq_sys_users_org_username" in str(getattr(error, "orig", error))
