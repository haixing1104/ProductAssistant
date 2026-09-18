"""依赖注入：DB 会话、当前用户、租户上下文、RBAC 守卫。

设计要点:
    · ``get_db`` 从 ``app.state.session_factory`` 取会话 —— 测试可注入自己的 factory（见 tests/conftest）；
    · ``get_current_user`` 把 JWT claims 变成 ``CurrentUser``，并把 ``org_id`` / ``user_id`` 写进
      ``request.state`` —— 仓储层据此**强制**加租户过滤（多租户隔离的落地方式）；
    · ``require_roles`` 是 RBAC 守卫工厂，用法：``Depends(require_roles("admin"))``。

为什么用 ``request.state`` 而不是全局 ContextVar:
    全局 ContextVar 在「并发请求 + 后台常驻任务（消费器）」共存时会被后台任务污染；
    ``request.state`` 天然与请求生命周期绑定，不会串租户。

平台超管（``is_superuser``）的唯一租户覆盖点（2026-09 新增，改前请先读）:
    ``sys_users.is_superuser`` 为真的账号可带 ``X-Org-Id`` 头把「当前租户」换成目标组织。
    覆盖**只发生在本模块**：``CurrentUser.org_id`` 是全仓唯一的租户事实源（所有 routers /
    services / repositories 都读它；``request.state.org_id`` 目前无人消费），因此
    **所有写路径、审批 CAS、生成投递、SSE 票据不需要任何改动** —— 这是
    「改动最小 + 多租户隔离红线一行不改」的关键设计。三条硬约束:
      ① 只有 ``is_superuser`` 为真才认这个头；普通用户带了 → 静默忽略 + 告警日志（不给探测信号）；
      ② 超管指定的目标组织必须存在且 ``status='active'``，否则 400 —— 比「悄悄退回归属组织」安全：
         后者会把数据写进错误的租户；而超管本就能列出全部组织（``GET /orgs``），不存在信息泄露；
      ③ 该标记只能由引导脚本写库（``tools/seed_super_admin.py``），**任何接口都不得设置它**。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import Organization, SysUser
from . import security

__all__ = [
    "SUPERUSER_ORG_HEADER",
    "CurrentUser",
    "bearer",
    "get_current_user",
    "get_db",
    "require_roles",
]

logger = logging.getLogger(__name__)

#: 平台超管覆盖「当前租户」的请求头（仅 ``is_superuser=true`` 时被信任；契约见模块 docstring）
SUPERUSER_ORG_HEADER = "x-org-id"

#: ``auto_error=False``：由我们自己抛 401（响应体统一走 errors.envelope）
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """已认证用户与租户上下文（全部来自服务端签发的 claims，不信任请求体）。

    注意:
        ``org_id`` 对平台超管可能已被 ``X-Org-Id`` 覆盖为**目标租户**（见模块 docstring），
        下游一律以它为「当前租户」，**无需感知超管的存在**；
        ``is_superuser`` 只给需要区分身份的极少数地方用（``GET /orgs`` 返回全量组织、
        前端据此决定是否显示组织选择器）。
    """

    id: str
    org_id: str
    username: str
    role: str
    is_superuser: bool = False


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


async def _resolve_effective_org(
    request: Request, session: AsyncSession, user: SysUser, *, claimed: str
) -> str:
    """决定本次请求的「生效租户」：平台超管可用 ``X-Org-Id`` 覆盖，其余一律用 ``claimed``。

    参数:
        request: 当前请求（读 ``X-Org-Id`` 头）。
        session: DB 会话（校验目标组织存在且启用）。
        user: 已认证用户行（读 ``is_superuser`` / ``username``）。
        claimed: 未覆盖时的租户（claims 优先、回退用户归属组织 —— 与历史行为一致）。
    返回:
        生效的 ``org_id`` 字符串（将写入 ``CurrentUser.org_id``）。
    异常:
        HTTPException: 400 —— 超管指定的目标组织「格式非法 / 不存在 / 已停用」。
    注意:
        · 普通用户带该头 → 一律**静默忽略**（记 warning）：既不让它变成探测 Oracle，
          也不改变任何既有客户端的行为；
        · 超管带非法目标 → **明确 400**，而不是悄悄退回归属组织：超管本就通过 ``GET /orgs``
          拿到组织列表，信息上没有额外暴露；而静默退回会把写操作落进错误的租户；
        · 目标 == 归属组织时不写「跨租户」日志（避免正常请求刷屏）。
    """
    raw = (request.headers.get(SUPERUSER_ORG_HEADER) or "").strip()
    if not raw:
        return claimed
    if not user.is_superuser:
        logger.warning("[scope] 忽略非超管的 %s 头: user=%s", SUPERUSER_ORG_HEADER, user.username)
        return claimed
    try:
        target = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="目标组织 ID 格式非法") from exc
    org = await session.get(Organization, target)
    if org is None or org.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="目标组织不存在或已停用")
    effective = str(org.id)
    if effective != claimed:
        # 跨租户操作留痕：令牌与 Redis 会话里只带归属组织，「本次以哪个租户身份操作」只能靠这条日志
        logger.info(
            "[scope] 超管跨租户操作: user=%s(%s) 归属=%s 目标=%s",
            user.username,
            user.id,
            claimed,
            effective,
        )
    return effective


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
        平台超管的 ``X-Org-Id`` 覆盖也在这里发生（见 ``_resolve_effective_org``）：
        覆盖后 ``CurrentUser.org_id`` 即「目标租户」，下游无需感知超管的存在。
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

    org_id = await _resolve_effective_org(
        request, session, user, claimed=str(claims.get("org_id") or user.org_id)
    )
    request.state.org_id = org_id
    request.state.user_id = str(user.id)
    return CurrentUser(
        id=str(user.id),
        org_id=org_id,
        username=user.username,
        role=user.role,
        is_superuser=bool(user.is_superuser),
    )


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