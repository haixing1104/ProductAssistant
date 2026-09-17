"""refresh 会话存储（Redis）：签发 / 轮换 / 吊销。

为什么 refresh 状态必须服务端可查（而不是无状态 JWT）:
    · 无状态 refresh 无法吊销 —— 用户登出、令牌被盗都只能等它自然过期；
    · **重用检测**需要「这个 token 是否已被用过」这一事实，只能存在服务端。

三把键（``core/keys.py`` 里的私有键）:
    · ``auth:refresh:{token}``      会话本体（值 = ``{user_id, org_id, role}`` JSON），TTL = refresh 有效期；
    · ``auth:refresh:owner:{token}`` token → user_id 反查；**会话被删除后仍然保留**，
      用于在「旧 token 二次出现」时定位被盗用户并整户吊销（没它就查不出是谁被盗）；
    · ``auth:refresh:user:{user_id}`` 用户 → 其全部 token 的集合（一键吊销用）。

轮换语义（与 ProductPilot 一致）:
    每次 refresh 都签发新 token 并作废旧的；若旧的（已作废）再次出现，视为被盗 → 吊销该用户全部会话。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..core import security
from ..core.config import Settings
from ..core.keys import RedisKeys

__all__ = ["RefreshSession", "issue", "resolve_and_rotate", "revoke_all_for_user", "revoke_token"]


@dataclass(frozen=True)
class RefreshSession:
    """一次 refresh 会话的身份三元组。"""

    user_id: str
    org_id: str
    role: str


def _ttl_seconds(settings: Settings) -> int:
    """refresh 有效期（秒）。"""
    return settings.refresh_token_expire_days * 86400


async def issue(redis_client, settings: Settings, *, user_id: str, org_id: str, role: str) -> str:
    """签发一个新的 refresh 会话并登记三把键。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        user_id: 用户 ID。
        org_id: 组织 ID。
        role: 角色（续签时写进新 access token，避免续签后角色丢失）。
    返回:
        新的 refresh token（由调用方写入 HttpOnly Cookie）。
    """
    keys = RedisKeys(settings.env)
    token = security.generate_refresh_token()
    ttl = _ttl_seconds(settings)
    await redis_client.set(
        keys.auth_refresh_session(token),
        json.dumps({"user_id": user_id, "org_id": org_id, "role": role}),
        ex=ttl,
    )
    await redis_client.set(keys.auth_refresh_owner(token), user_id, ex=ttl)
    user_set = keys.auth_refresh_user_set(user_id)
    await redis_client.sadd(user_set, token)
    await redis_client.expire(user_set, ttl)
    return token


async def resolve_and_rotate(redis_client, settings: Settings, token: str) -> RefreshSession | None:
    """校验 refresh token 并立即轮换（旧 token 作废）。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        token: 客户端 Cookie 里带来的 refresh token。
    返回:
        新的会话身份（``RefreshSession``）；token 无效或已重用 → None。
    注意:
        **重用检测**：会话键不存在但 owner 键还在 = 这个 token 曾经有效、已被轮换或登出后又被使用
        → 按被盗处理，吊销该用户全部会话（宁可误伤一次登录，也不放过令牌泄露）。
    """
    keys = RedisKeys(settings.env)
    raw = await redis_client.get(keys.auth_refresh_session(token))
    if raw is None:
        owner = await redis_client.get(keys.auth_refresh_owner(token))
        if owner:
            await revoke_all_for_user(redis_client, settings, owner)
        return None
    claims = json.loads(raw)
    session = RefreshSession(user_id=claims["user_id"], org_id=claims["org_id"], role=claims["role"])
    # 先删旧再发新：中间失败最多让用户重登一次，绝不会出现「两个有效 token」
    await revoke_token(redis_client, settings, token)
    return session


async def revoke_token(redis_client, settings: Settings, token: str) -> None:
    """作废单个 refresh 会话（保留 owner 键以支持重用检测）。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        token: 目标 token。
    返回:
        无返回值。
    """
    keys = RedisKeys(settings.env)
    raw = await redis_client.get(keys.auth_refresh_session(token))
    await redis_client.delete(keys.auth_refresh_session(token))
    if raw:
        try:
            user_id = json.loads(raw)["user_id"]
        except (KeyError, ValueError):
            return
        await redis_client.srem(keys.auth_refresh_user_set(user_id), token)


async def revoke_all_for_user(redis_client, settings: Settings, user_id: str) -> int:
    """吊销某用户的全部 refresh 会话（登出全端 / 令牌被盗响应）。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        user_id: 用户 ID。
    返回:
        被作废的会话数。
    """
    keys = RedisKeys(settings.env)
    user_set = keys.auth_refresh_user_set(user_id)
    tokens = await redis_client.smembers(user_set)
    for token in tokens:
        await redis_client.delete(keys.auth_refresh_session(token), keys.auth_refresh_owner(token))
    await redis_client.delete(user_set)
    return len(tokens)
