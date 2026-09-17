"""依赖注入：DB 会话、当前用户、租户上下文、RBAC 守卫。

设计要点:
    · ``get_db`` 从 ``app.state.session_factory`` 取会话 —— 测试可注入自己的 factory（见 tests/conftest）；
    · ``get_current_user`` 把 JWT claims 变成 ``CurrentUser``，并把 ``org_id`` / ``user_id`` 写进
      ``request.state`` —— 仓储层据此**强制**加租户过滤（多租户隔离的落地方式）；
    · ``require_roles`` 是 RBAC 守卫工厂，用法：``Depends(require_roles("admin"))``。

为什么用 ``request.state`` 而不是全局 ContextVar:
    全局 ContextVar 在「并发请求 + 后台常驻任务（消费器）」共存时会被后台任务污染；
    ``request.state`` 天然与请求生命周期绑定，不会串租户。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import SysUser
from . import security

__all__ = ["CurrentUser", "bearer", "get_current_user", "get_db", "require_roles"]

#: ``auto_error=False``：由我们自己抛 401（响应体统一走 errors.envelope）
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """已认证用户与租户上下文（全部来自服务端签发的 claims，不信任请求体）。"""

    id: str
    org_id: str
    username: str
    role: str


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """按请求提供 DB 会话（session 生命周期 = 请求生命周期）。

    注意:
        本函数**不自动 commit**：写操作由服务层显式 ``commit``（一次业务事务边界清晰可见），
        避免「路由里改了对象却因为没 commit 而静默丢失」。
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def _require_bearer(cred: HTTPAuthorizationCredentials | None) -> str:
    """取 Bearer 令牌（缺失/方案不符 → 401）。"""
    if cred is None or cred.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    return cred.credentials


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db),
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> CurrentUser:
    """解码 JWT → 载入用户 → 注入租户上下文。

    参数:
        request: 当前请求（读 ``app.state.settings``；写 ``request.state.org_id/user_id``）。
        session: DB 会话。
        cred: Bearer 凭证。
    返回:
        ``CurrentUser``。
    异常:
        HTTPException: 401（令牌无效/过期、用户不存在或已停用）。
    注意:
        每次请求都查一次 ``sys_users``：多一次查询换来「用户被停用后立即失效」——
        JWT 短时效（15min）已经限制了窗口，但停用要**立刻**生效（安全 > 一次索引查询）。
    """
    settings = request.app.state.settings
    token = _require_bearer(cred)
    try:
        claims = security.decode_token(token, settings)
    except Exception as exc:  # noqa: BLE001 JWTError / ExpiredSignatureError 统一 401
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期") from exc

    user = (await session.execute(select(SysUser).where(SysUser.id == claims["sub"]))).scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")

    org_id = str(claims.get("org_id") or user.org_id)
    request.state.org_id = org_id
    request.state.user_id = str(user.id)
    return CurrentUser(id=str(user.id), org_id=org_id, username=user.username, role=user.role)


def require_roles(*roles: str):
    """RBAC 守卫工厂：仅允许列出的角色访问。

    参数:
        roles: 允许的角色名（``admin`` / ``reviewer`` / ``operator``）。
    返回:
        依赖函数（``Depends(require_roles("admin"))`` 使用）。
    注意:
        角色判断用的是 ``CurrentUser.role``，而它来自**每次请求查库得到的 ``sys_users.role``**
        （``get_current_user`` 里赋值），不是 JWT 里的 ``role`` claim。这条口径与
        「停用用户立即失效」同一取舍：**改角色立即生效**，代价是每请求一次索引查询。
        因此测试里要验证「非 admin 被拒」，必须造**库里真是该角色**的用户
        （只改 token claim 无效 —— 见 ``tests/test_members_rbac.py`` 的做法）。
    """

    async def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        """执行角色校验。"""
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前角色无权执行该操作")
        return user

    return _guard