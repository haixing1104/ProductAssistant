"""认证路由：注册 / 登录 / 续签（轮换）/ 登出 / 当前身份。

会话模型（生产式）:
    · **access**：短时效 JWT（默认 15min），前端仅内存持有，随请求头 ``Authorization`` 传递；
    · **refresh**：随机串 + 服务端状态（Redis），HttpOnly + SameSite=Lax Cookie，前端 JS 读不到；
    · **CSRF 双保险**：refresh 必须带 ``X-Requested-With: fetch``
      —— 纯前端跨站表单请求带不了自定义头，能挡住最朴素的 Cookie 滥用；
    · Cookie 作用域限定 ``/api/v1/auth``：其它接口根本收不到 refresh，泄露面最小。

为什么登录不返回 refresh token:
    返回给 JS 就等于放弃了 HttpOnly 的全部收益。refresh 只走 Set-Cookie。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import security
from ..core.config import Settings
from ..core.deps import CurrentUser, get_current_user, get_db
from ..core.redis_client import new_async_redis
from ..models.orm import Organization
from ..schemas.api import CurrentUserResponse, LoginRequest, RegisterRequest, TokenResponse
from ..services import session_store
from ..services.auth_service import INVALID_CREDENTIALS_DETAIL, AuthService
from ..services.login_guard import check_locked, clear_failures, client_ip, record_failure
from .common import ok

__all__ = ["router"]

router = APIRouter(prefix="/auth", tags=["auth"])

#: refresh Cookie 的作用域：只有认证接口需要带上它
REFRESH_COOKIE_PATH = "/api/v1/auth"
#: CSRF 双保险要求的请求头（与前端 http.ts 的约定一致）
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "fetch"


def _set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    """写入 refresh Cookie（HttpOnly + SameSite=Lax + 可选 Secure）。"""
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """删除 refresh Cookie（登出）。"""
    response.delete_cookie(settings.refresh_cookie_name, path=REFRESH_COOKIE_PATH)


def _token_payload(user_id: str, org_id: str, role: str, settings: Settings) -> dict:
    """构造 access 响应体（refresh 不出现在响应体，见模块 docstring）。"""
    return TokenResponse(
        access_token=security.create_access_token(
            user_id=user_id, org_id=org_id, role=role, settings=settings
        ),
        expires_in=settings.access_token_expire_minutes * 60,
    ).model_dump()


@router.post("/register")
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_db)) -> dict:
    """注册 = 创建新租户 + 该租户的首个 admin（不自动登录，前端跳登录页）。"""
    user = await AuthService(session).register(
        org_name=body.org_name, username=body.username, password=body.password
    )
    await session.commit()
    return ok({"user_id": str(user.id), "org_id": str(user.org_id), "role": user.role})


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    """当前登录身份（id / org_id / username / role），供前端角色化 UI。"""
    return ok(
        CurrentUserResponse(
            id=user.id, org_id=user.org_id, username=user.username, role=user.role
        ).model_dump()
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """登录：限流检查 → 校验口令 → 组织状态检查 → 签发 access/refresh。

    流程顺序是刻意的:
        ① 先查锁定（避免用 bcrypt 校验去消耗 CPU）；
        ② 口令错误 → 记一次失败并返回统一 401（不区分用户不存在/口令错）；
        ③ 口令正确但组织被停用 → 403（此时已认证，可以告知原因）；
        ④ 成功 → 清空失败计数 + 更新 ``last_login_at`` + 下发 Cookie。
    """
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    redis_client = new_async_redis(settings)
    try:
        locked = await check_locked(redis_client, settings, username=body.username, ip=ip)
        if locked.locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录失败次数过多，请稍后再试",
                headers={"Retry-After": str(locked.retry_after)},
            )
        user = await AuthService(session).authenticate(
            username=body.username, password=body.password, org_name=body.org_name
        )
        if user is None:
            await record_failure(redis_client, settings, username=body.username, ip=ip)
            await session.rollback()  # 丢弃失败尝试可能造成的任何变更
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS_DETAIL)
        org = await session.get(Organization, user.org_id)
        if org is None or org.status != "active":
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="所属组织已停用")
        await clear_failures(redis_client, settings, username=body.username, ip=ip)
        refresh_token = await session_store.issue(
            redis_client, settings, user_id=str(user.id), org_id=str(user.org_id), role=user.role
        )
        payload = _token_payload(str(user.id), str(user.org_id), user.role, settings)
    finally:
        await redis_client.aclose()
    await session.commit()
    _set_refresh_cookie(response, settings, refresh_token)
    return ok(payload)


@router.post("/refresh")
async def refresh(request: Request, response: Response) -> dict:
    """续签：Cookie 里的 refresh → 新 access + 新 refresh（轮换 + 重用检测）。"""
    settings: Settings = request.app.state.settings
    if (request.headers.get(CSRF_HEADER) or "").lower() != CSRF_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少安全请求头（CSRF 防护）")
    token = request.cookies.get(settings.refresh_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 refresh cookie")
    redis_client = new_async_redis(settings)
    try:
        rotate = await session_store.resolve_and_rotate(redis_client, settings, token)
        if rotate is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh token 无效或已重用，会话已吊销",
            )
        new_refresh = await session_store.issue(
            redis_client,
            settings,
            user_id=rotate.user_id,
            org_id=rotate.org_id,
            role=rotate.role,
        )
        payload = _token_payload(rotate.user_id, rotate.org_id, rotate.role, settings)
    finally:
        await redis_client.aclose()
    _set_refresh_cookie(response, settings, new_refresh)
    return ok(payload)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """登出：作废服务端 refresh 会话并清 Cookie（access 靠短时效自然失效）。"""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.refresh_cookie_name)
    if token:
        redis_client = new_async_redis(settings)
        try:
            await session_store.revoke_token(redis_client, settings, token)
        finally:
            await redis_client.aclose()
    _clear_refresh_cookie(response, settings)
    return ok({})
