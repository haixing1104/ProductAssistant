"""注册与登录服务。

两个 PA 专属要点（相对 ProductPilot 的改进，改前请先读）:
    ① **同名账号跨组织消歧**：``sys_users`` 的唯一键是 ``(org_id, username)``
       （`0001_schema.sql`），即用户名只在组织内唯一。ProductPilot 用
       ``where(username=…).scalar_one_or_none()`` 会直接抛 ``MultipleResultsFound``。
       本实现改为：取候选 → 校验口令 → 恰好命中一个才算登录成功；命中多个（同名同口令）
       才报 400 让用户提供 ``org_name``。**正常用户完全无感知**。
    ② ``last_login_at`` 落库：PA 的 ``sys_users`` 有该列且前端要展示「最近登录」，
       登录成功即更新（失败不更新，避免把失败尝试伪装成活动时间）。

安全口径:
    · 用户名不存在 / 口令错误 / 账号停用 → 一律返回同一句「用户名或密码错误」（不区分，防枚举）；
    · bcrypt 校验有成本（~100ms），候选数上限 3 —— 防止「同名账号特别多」时被拿来放大 CPU（DoS）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import security
from ..core.errors import ApiError
from ..models.orm import Organization, SysUser

__all__ = ["AuthService", "MAX_CANDIDATES", "INVALID_CREDENTIALS_DETAIL"]

#: 口令校验候选上限（bcrypt 慢，候选越多越容易被放大量）
MAX_CANDIDATES = 3

#: 统一的登录失败文案（不区分用户不存在/口令错误/账号停用，防账号枚举）
INVALID_CREDENTIALS_DETAIL = "用户名或密码错误"


class AuthService:
    """认证服务（注册 = 新租户 + 首个 admin；登录 = 跨组织候选消歧）。"""

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        参数:
            session: DB 会话（登录发生在租户确定之前，故**不带** org 上下文）。
        """
        self.session = session

    async def register(self, *, org_name: str, username: str, password: str) -> SysUser:
        """注册即开租户：建 ``organizations`` + 首个 ``admin`` 用户。

        参数:
            org_name: 组织名称。
            username: 登录名。
            password: 明文口令（本函数内做 bcrypt，明文不落库、不进日志）。
        返回:
            新建的 ``SysUser``（已 flush，含 id/org_id）。
        """
        org = Organization(name=org_name, status="active")
        self.session.add(org)
        await self.session.flush()
        user = SysUser(
            org_id=org.id,
            username=username,
            hashed_password=security.hash_password(password),
            role="admin",
            status="active",
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def authenticate(
        self, *, username: str, password: str, org_name: str | None = None
    ) -> SysUser | None:
        """按用户名（可选组织名）校验口令。

        参数:
            username: 登录名。
            password: 明文口令。
            org_name: 组织名（同名账号跨组织时的消歧项；None 表示未提供）。
        返回:
            校验通过且处于 active 的 ``SysUser``；用户不存在/口令错误/已停用 → None。
        异常:
            ApiError(400): 候选账号无法唯一确定（同名同口令跨组织），需要更多信息。
        注意:
            本函数**不判断组织状态**（``organizations.status`` 是否 suspended）——
            该判断在路由层做完口令校验后再做，避免把「组织被停用」这一信息暴露给未认证请求。
        """
        candidates = (
            (
                await self.session.execute(
                    select(SysUser, Organization.name)
                    .join(Organization, SysUser.org_id == Organization.id)
                    .where(SysUser.username == username, SysUser.status == "active")
                    .order_by(SysUser.created_at)
                    .limit(MAX_CANDIDATES + 1)
                )
            )
            .all()
        )
        if not candidates:
            return None

        if org_name:
            filtered = [row for row in candidates if row[1] == org_name]
            # 过滤后为空 = 组织名写错 → 与「账号不存在」同一响应（不告诉调用方组织名是否正确）
            if not filtered:
                return None
            candidates = filtered

        matched = [row for row in candidates if security.verify_password(password, row[0].hashed_password)]
        if not matched:
            return None
        if len(matched) > 1:
            unique_orgs = {str(row[0].org_id) for row in matched}
            if len(unique_orgs) > 1:
                raise ApiError(
                    400,
                    "同名账号在多个组织中且口令相同，无法确定登录身份：请在登录时提供 org_name",
                )
        user = matched[0][0]
        user.last_login_at = _now()
        await self.session.flush()
        return user


def _now():
    """当前时间（``timezone-aware``；单独抽出便于测试替换与统一口径）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
