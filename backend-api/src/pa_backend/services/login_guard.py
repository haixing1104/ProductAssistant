"""登录失败限流与临时锁定（Redis 计数）。

威胁模型与取舍（与 ProductPilot 同口径，PA 侧键名改为 ``auth:`` 命名空间）:
    · **单账号爆破（撞库）**：同一「用户名 + 客户端 IP」在窗口内累计失败达阈值即锁定，
      验密之前直接 429（连 bcrypt 都不做，避免 CPU 被拿来放大）；
    · **同源广撒**：计数维度取 ``(username, ip)`` 组合 —— 避免「一个人反复输错密码」
      把同出口 IP 的整间办公室一起锁死；
    · **成功即清零**：正常用户忘密码几次后输对，不应继续被历史失败拖累；
    · **fail-open**：Redis 异常时放行。限流是加固而非登录的必要条件 ——
      不能让限流组件故障演变成「全站无法登录」（客户端已配 3s/10s 超时，不会无限挂起）。

键位（backend 私有，见 ``core/keys.py``）:
    ``pa:{env}:auth:loginfail:{username}:{ip}``（窗口内计数）
    ``pa:{env}:auth:loginlock:{username}:{ip}``（存在即锁定中）
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..core.config import Settings
from ..core.keys import RedisKeys

__all__ = ["LockState", "check_locked", "clear_failures", "client_ip", "record_failure"]


def client_ip(request: Request) -> str:
    """取真实客户端 IP（优先 ``X-Forwarded-For`` 首跳）。

    参数:
        request: 入站请求。
    返回:
        IP 字符串（取不到时返回 ``"unknown"``）。
    注意:
        XFF 可被伪造，但它在这里只是「缩小爆破面」的辅助维度（主维度是用户名）：
        伪造它只能改变锁定分组，绕不开「同一用户名 + 同源」的累计判定，也不会误伤他人。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def login_subject(username: str, ip: str) -> str:
    """计数主体：``username:ip``（用户名统一小写，``Admin`` 与 ``admin`` 不因大小写绕过计数）。"""
    return f"{username.strip().lower()}:{ip}"


@dataclass(frozen=True)
class LockState:
    """锁定状态；``retry_after`` 为建议重试秒数（回填 ``Retry-After`` 响应头）。"""

    locked: bool
    retry_after: int = 0


async def check_locked(redis_client, settings: Settings, *, username: str, ip: str) -> LockState:
    """检查是否处于锁定期；Redis 异常一律视为未锁定（fail-open）。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        username: 登录名。
        ip: 客户端 IP。
    返回:
        ``LockState``。
    """
    keys = RedisKeys(settings.env)
    try:
        ttl = await redis_client.ttl(keys.auth_login_lock(login_subject(username, ip)))
    except Exception:  # noqa: BLE001 限流组件故障不得阻断登录
        return LockState(locked=False)
    if ttl is None or ttl < 0:  # -2 键不存在 / -1 无过期时间：均视为未锁定
        return LockState(locked=False)
    return LockState(locked=True, retry_after=max(1, int(ttl)))


async def record_failure(redis_client, settings: Settings, *, username: str, ip: str) -> bool:
    """记一次登录失败；窗口内累计达阈值即转锁定并清零计数。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        username: 登录名。
        ip: 客户端 IP。
    返回:
        本次是否触发锁定（True = 已进入锁定期）。
    注意:
        仅**首次**失败设置窗口 TTL —— 窗口从「第一次失败」起算，中途失败不会无限续期
        （否则持续攻击会把窗口一直往后推，等于永不失效的累积）。
    """
    keys = RedisKeys(settings.env)
    window_seconds = settings.login_failure_window_seconds
    try:
        fail_key = keys.auth_login_fail(login_subject(username, ip))
        count = await redis_client.incr(fail_key)
        if count == 1:
            await redis_client.expire(fail_key, window_seconds)
        if count >= settings.login_max_failures:
            await redis_client.set(
                keys.auth_login_lock(login_subject(username, ip)),
                str(count),
                ex=settings.login_lockout_seconds,
            )
            await redis_client.delete(fail_key)
            return True
    except Exception:  # noqa: BLE001 计数失败不阻断登录流程
        return False
    return False


async def clear_failures(redis_client, settings: Settings, *, username: str, ip: str) -> None:
    """登录成功：清零失败计数与锁定标记。

    参数:
        redis_client: 异步 Redis 客户端。
        settings: 应用配置。
        username: 登录名。
        ip: 客户端 IP。
    返回:
        无返回值。
    """
    keys = RedisKeys(settings.env)
    subject = login_subject(username, ip)
    try:
        await redis_client.delete(keys.auth_login_fail(subject), keys.auth_login_lock(subject))
    except Exception:  # noqa: BLE001 清理失败只影响下次计数起点，不影响本次登录
        pass
