"""SSE 事件读取器：从 ``pa:{env}:evt:{thread_id}`` 回放与增量尾随。

为什么用**同步** redis 客户端（而不是本模块已有的 aioredis）:
    SSE 是长生命周期流式生成器，要在其中反复轮询 Redis。实测（ProductPilot 的踩坑记录）：
    aioredis 在长连接生成器内多次轮询会读到 0 条（连接被回收/事件循环调度问题），
    而「每次短连接同步读取」稳定。因此本模块用同步客户端，由调用方经 ``asyncio.to_thread``
    丢进线程池执行，既稳定又不阻塞事件循环。

数据源唯一:
    回放与实时尾随**读的是同一条流**（`XRange` 带游标），因此不存在「Pub/Sub 双通道去重」的问题：
    SSE 的 ``id:`` 就是 Redis Stream 的消息 ID，天然单调递增，可直接作为 ``Last-Event-ID`` 的续传游标。

游标语义（``XRange`` 的边界，容易写错）:
    · ``min="-"``        → 从头（``0-0``）开始，含第一条；
    · ``min="(<msg_id>)"`` → **不含**该 ID 的后续消息（续传时的正确写法，否则会重复发一条事件）。
"""

from __future__ import annotations

import logging

import redis

from ..core.config import Settings
from ..core.keys import RedisKeys
from .event_envelope import decode_fields

__all__ = ["EvtStreamReader", "TERMINAL_EVENT_TYPES"]

logger = logging.getLogger(__name__)

#: 关流判据：收到任一终态事件即结束本次连接（前端据此复位 UI）
#: · ``done`` / ``rejected`` / ``failed``：图终态（ai-engine ``worker`` / 节点发布）；
#: · ``hitl.waiting``：**PA 特有** —— 图已挂起等待人工审批，可能持续数小时，
#:   长连接没有意义；关流后前端显示「待审批」，审批通过后（新线程/恢复结果）再按需重连。
TERMINAL_EVENT_TYPES = frozenset({"done", "rejected", "failed", "hitl.waiting"})


class EvtStreamReader:
    """单个 SSE 连接私有的读取器（一条连接一个实例，用完 ``close()``）。

    每次调用只做一次 ``XRange``（数量受限），不做阻塞读 —— 轮询节奏由路由层控制，
    这样空闲关流与终态关流的判定都在路由层，逻辑集中在一处。
    """

    def __init__(self, settings: Settings) -> None:
        """初始化。

        参数:
            settings: 应用配置（读 ``redis_url`` / ``env``）。
        """
        self._keys = RedisKeys(settings.env)
        self._client: redis.Redis | None = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )

    @staticmethod
    def _min_cursor(last_id: str | None) -> str:
        """把「上次收到的消息 ID」转成 ``XRange`` 的下界（见模块 docstring 的游标语义）。"""
        return f"({last_id}" if last_id else "-"

    def _read(self, thread_id: str, last_id: str | None, count: int) -> list[dict]:
        """读一批事件（同步；调用方负责放进线程池）。

        参数:
            thread_id: 线程 ID。
            last_id: 上次已发送的消息 ID（None = 从头回放）。
            count: 单次上限（防止一次拉太多把响应撑爆）。
        返回:
            ``[{"seq": <stream id>, "payload": {...}}, …]``（按流内顺序）。
        异常:
            redis.RedisError: 连接/超时（由路由层决定：中断连接让前端带 Last-Event-ID 重连）。
        """
        client = self._client
        if client is None:
            return []
        entries = client.xrange(
            self._keys.evt(thread_id), min=self._min_cursor(last_id), max="+", count=count
        )
        return [{"seq": str(message_id), "payload": decode_fields(fields)} for message_id, fields in entries]

    def history(self, thread_id: str, *, last_id: str | None = None, count: int = 200) -> list[dict]:
        """回放历史事件（连接建立时调用一次）。"""
        return self._read(thread_id, last_id, count)

    def poll(self, thread_id: str, *, last_id: str | None, count: int = 200) -> list[dict]:
        """增量读取（尾随阶段反复调用）。"""
        return self._read(thread_id, last_id, count)

    def close(self) -> None:
        """关闭连接（幂等；连接池随进程回收）。"""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 关闭失败不影响前端已收到的数据
                logger.warning("关闭 SSE Redis 连接失败: %r", exc)
