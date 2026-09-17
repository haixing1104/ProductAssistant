"""Redis 连接工厂（同步 + 异步两套）。

为什么两套都要有:
    · **异步**（``redis.asyncio``）：常规请求路径（refresh 轮换、登录限流、XADD 投递 job）；
    · **同步**（``redis``）：SSE 长连接内的增量轮询（P5 落地，见 ProductPilot 的实测结论：
      aioredis 在长生命周期流式生成器内多次轮询会读到 0，同步短连接稳定）与
      ``WorkflowResultConsumer`` 的独立消费循环（避免与请求事件循环争用）。

超时口径（刻意收紧）:
    connect 3s / socket 10s —— 基础设施不可达时必须**快速失败**：限流组件故障要 fail-open，
    消费器要靠异常退避重试，任何一处都不允许无限挂起把连接池耗尽。

所有权:
    工厂只创建客户端，**不负责关闭**（调用方用 ``try/finally`` 或上下文关闭，见各调用点）。
"""

from __future__ import annotations

import redis
import redis.asyncio as aioredis

from .config import Settings

#: 连接超时（秒）：不可达时快速失败，避免把连接池挂满
CONNECT_TIMEOUT_SECONDS = 3.0
#: 读写超时（秒）：SSE 轮询/消费循环都不允许长时间阻塞
SOCKET_TIMEOUT_SECONDS = 10.0

__all__ = ["new_async_redis", "new_sync_redis"]


def new_async_redis(settings: Settings) -> aioredis.Redis:
    """创建异步客户端（请求路径用）。

    参数:
        settings: 应用配置（读 ``redis_url``）。
    返回:
        ``redis.asyncio.Redis``（默认 ``decode_responses=True``，取回的即是 str）。
    """
    return aioredis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
    )


def new_sync_redis(settings: Settings) -> redis.Redis:
    """创建同步客户端（消费循环 / SSE 轮询用）。

    参数:
        settings: 应用配置（读 ``redis_url``）。
    返回:
        ``redis.Redis``（默认 ``decode_responses=True``）。
    """
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
    )
