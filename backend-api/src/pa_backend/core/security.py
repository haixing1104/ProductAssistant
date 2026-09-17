"""安全工具：口令哈希、JWT 签发/校验、refresh 会话标识、两类短时票据。

三类凭证，用途与 TTL 完全不同（不要混用）:
    ① ``access``（``kind`` 缺省）：请求鉴权，短时效（``BACKEND_ACCESS_TOKEN_MINUTES``），前端仅内存持有；
    ② ``sse`` 票据：SSE 长连接专用，绑定 ``product_id``，短时效（``BACKEND_STREAM_TICKET_TTL_SECONDS``）
       —— 目的：**长连接不与短时效 access 耦合**（access 15 分钟到期就会把 3 分钟的流打断）；
    ③ ``approval_redirect`` 深链票据：通知（钉钉/邮件）里的「去审批」跳转凭证，长时效（天），
       只携带「跳到哪个待办」，**不授予任何审批权限**（真正审批仍要登录态 + CAS）。

``org_id`` 写进 claims 的意义:
    它不是「客户端说了算的入参」，而是**查询层强制过滤的数据来源**（多租户隔离的根）。
    因此 ``get_current_user`` 会用 claims 里的 org_id 覆盖请求上下文，仓储层一律以它过滤。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import Settings

__all__ = [
    "create_access_token",
    "create_approval_ticket",
    "create_sse_ticket",
    "decode_token",
    "generate_refresh_token",
    "hash_password",
    "verify_password",
]

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

#: 票据种类标记（解码后必须校验 kind，防止「用 SSE 票据调审批接口」这类越权）
KIND_SSE = "sse"
KIND_APPROVAL_REDIRECT = "approval_redirect"


def hash_password(plain: str) -> str:
    """bcrypt 哈希（salt 由 passlib 自动生成并编码在结果里）。"""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验口令（哈希格式非法时返回 False，不抛异常）。"""
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001 库在脏哈希（历史数据/手工改库）上会抛，统一按校验失败处理
        return False


def generate_refresh_token() -> str:
    """签发 refresh 会话标识（随机串；**状态存 Redis**，因此可轮换、可吊销）。"""
    return secrets.token_urlsafe(48)


def _encode(payload: dict[str, Any], settings: Settings) -> str:
    """统一签发入口（自动补 ``iat``）。"""
    now = datetime.now(timezone.utc)
    return jwt.encode({**payload, "iat": now}, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(*, user_id: str, org_id: str, role: str, settings: Settings) -> str:
    """签发 access token。

    参数:
        user_id: 用户 ID（写入 ``sub``）。
        org_id: 组织 ID（**多租户过滤的唯一来源**）。
        role: 角色（admin/reviewer/operator，供 RBAC 守卫）。
        settings: 应用配置（读密钥/算法/TTL）。
    返回:
        JWT 字符串。
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return _encode(
        {"sub": user_id, "org_id": org_id, "role": role, "exp": expire},
        settings,
    )


def create_sse_ticket(*, user_id: str, org_id: str, role: str, product_id: str, settings: Settings) -> str:
    """签发 SSE 短时票据（P5 端点使用；当前仅有单元测试覆盖）。

    参数:
        user_id: 用户 ID。
        org_id: 组织 ID。
        role: 角色。
        product_id: **绑定到具体商品**——票据泄露也只能看这一个商品的流。
        settings: 应用配置（读票据 TTL）。
    返回:
        携带 ``kind="sse"`` 的 JWT。
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.stream_ticket_ttl_seconds)
    return _encode(
        {
            "sub": user_id,
            "org_id": org_id,
            "role": role,
            "product_id": product_id,
            "kind": KIND_SSE,
            "exp": expire,
        },
        settings,
    )


def create_approval_ticket(
    *, approval_id: str, org_id: str, product_id: str, settings: Settings, ttl_days: int | None = None
) -> str:
    """签发审批深链票据（通知摘要里附带的跳转凭证）。

    参数:
        approval_id: 待办 ID。
        org_id: 组织 ID（防跨租户跳转）。
        product_id: 商品 ID（前端可据此预热详情页）。
        settings: 应用配置。
        ttl_days: 覆盖默认 TTL（天）；None 用 ``BACKEND_APPROVAL_TICKET_TTL_DAYS``。
    返回:
        携带 ``kind="approval_redirect"`` 的 JWT。
    """
    days = ttl_days if ttl_days is not None else settings.approval_ticket_ttl_days
    expire = datetime.now(timezone.utc) + timedelta(days=days)
    return _encode(
        {
            "approval_id": approval_id,
            "org_id": org_id,
            "product_id": product_id,
            "kind": KIND_APPROVAL_REDIRECT,
            "exp": expire,
        },
        settings,
    )


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """校验并解码 JWT（失败抛 ``jose.JWTError`` 的子类，由调用方转 401/403）。

    参数:
        token: JWT 字符串。
        settings: 应用配置（读密钥/算法）。
    返回:
        claims 字典。
    异常:
        JWTError: 签名不符 / 过期 / 格式非法。
    """
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


#: 供调用方 ``except`` 用的类型别名（保持 jose 依赖只在本模块出现）
TokenError = JWTError
