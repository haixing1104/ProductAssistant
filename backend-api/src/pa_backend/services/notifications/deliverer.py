"""outbox 投递器：带重试 / 指数退避 / DLQ 的常驻投递。

状态机（``notification_outbox.status``）::

    pending ──发送成功──► sent（终态，记录 provider_msg_id）
       │
       └──发送失败──► retry_count+1 且 < max_attempts ──► pending（next_retry_at = now + 退避）
                              │
                              └──► 达到 max_attempts ──► dlq（终态，需人工介入）

两条刻意设计:
    · **领取即推进**：单进程投递（backend 单实例），不用 DB 锁；重复投递的代价远小于漏投；
    · **没有 sender 的渠道直接 DLQ**：``live`` 模式下「有渠道名但缺凭据」属配置错误，
      无脑重试只会刷屏；判 DLQ 并在日志点明原因，让运维一眼看到。

可观测性（失败原因为什么落库）:
    失败时把原因写进 ``payload['last_error']``（诊断字段）。只打日志是不够的：
    审批中心要能直接回答「通知为什么没到」——「群机器人 webhook 填错」和「网络抖动」
    的处置方式完全不同，而这两者在界面上通常长得一样（都是「没发出去」）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...core.config import Settings
from ...models.orm import NotificationOutbox
from .adapters import NotificationSendError, Sender

__all__ = ["OutboxDeliverer"]

#: 最大投递尝试次数（含首次）；超过即 DLQ
DEFAULT_MAX_ATTEMPTS = 3
#: 退避基数（秒）：第 n 次失败后等待 ``base * 2**(n-1)``
DEFAULT_BACKOFF_SECONDS = 30
#: 常驻循环轮询间隔（秒）
POLL_INTERVAL_SECONDS = 5.0
#: ``payload['last_error']`` 的最大长度（避免把整段 SMTP/HTTP 交互写进 jsonb）
MAX_ERROR_CHARS = 500


def _clip_error(exc: BaseException | str) -> str:
    """把异常/文本裁成可入库的简短原因。"""
    text = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    return text[:MAX_ERROR_CHARS]


class OutboxDeliverer:
    """消费 ``notification_outbox`` 的待投递行。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        senders: dict[str, Sender],
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: int = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        """初始化。

        参数:
            session_factory: 会话工厂（每批一个事务）。
            settings: 应用配置（保留：便于日志带上 env）。
            senders: ``{渠道名: sender}``（来自 ``resolver.build_senders``）。
            max_attempts: 最大尝试次数（达到即 DLQ）。
            backoff_seconds: 退避基数（秒）。
        """
        self._session_factory = session_factory
        self._settings = settings
        self._senders = dict(senders)
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(1, backoff_seconds)

    async def process_due_once(self, *, limit: int = 20, now: datetime | None = None) -> int:
        """处理一批到期记录（``pending`` 且 ``next_retry_at <= now``）。

        参数:
            limit: 单批上限（避免一次拉起过多外部请求）。
            now: 时间基准（默认当前 UTC；测试可注入）。
        返回:
            实际处理的条数。
        """
        moment = now or datetime.now(timezone.utc)
        processed = 0
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.status == "pending",
                            (NotificationOutbox.next_retry_at.is_(None))
                            | (NotificationOutbox.next_retry_at <= moment),
                        )
                        .order_by(NotificationOutbox.created_at)
                        .limit(max(1, limit))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                self._deliver(session, row, now=moment)
                processed += 1
            await session.commit()
        return processed

    def _deliver(self, session: AsyncSession, row: NotificationOutbox, *, now: datetime) -> None:
        """投递单条并把结果写回该行（成功 sent / 失败退避或 DLQ）。

        参数:
            session: 当前事务会话。
            row: 待投递行。
            now: 时间基准。
        返回:
            无返回值。
        """
        sender = self._senders.get(row.channel)
        if sender is None:
            row.status = "dlq"
            row.payload = {**(row.payload or {}), "last_error": f"渠道 {row.channel!r} 没有可用 sender"}
            print(
                f"[notify] 渠道 {row.channel!r} 没有可用 sender（live 模式缺凭据？）"
                f" id={row.id} → 标记 dlq"
            )
            return
        try:
            provider_msg_id = sender.send(row.payload)
        except NotificationSendError as exc:
            row.retry_count += 1
            # 失败原因落库（payload['last_error']，诊断字段）：否则接口只能告诉审批人
            # 「投递失败」，却答不出「为什么失败」—— 而这两种失败（地址错 vs 网络抖）的处理方式完全不同。
            # 注意：重新赋值而非原地改 dict —— 原地改 JSONB 需要 flag_modified 才会被持久化。
            row.payload = {**(row.payload or {}), "last_error": _clip_error(exc)}
            if row.retry_count >= self._max_attempts:
                row.status = "dlq"
                print(f"[notify] 投递失败达上限 id={row.id} channel={row.channel} → dlq: {exc}")
            else:
                row.status = "pending"
                row.next_retry_at = now + timedelta(seconds=self._backoff_seconds * (2 ** (row.retry_count - 1)))
                print(
                    f"[notify] 投递失败 id={row.id} channel={row.channel} 第 {row.retry_count} 次，"
                    f"下次重试 {row.next_retry_at.isoformat()}: {exc}"
                )
            return
        row.status = "sent"
        row.provider_msg_id = provider_msg_id
        row.next_retry_at = None

    async def run(self, stop: asyncio.Event, *, interval: float = POLL_INTERVAL_SECONDS) -> None:
        """常驻循环（lifespan 启动）。

        参数:
            stop: 停止信号。
            interval: 轮询间隔（秒）。
        返回:
            无返回值。
        """
        print(
            f"[notify] env={self._settings.env} 投递器启动（渠道={list(self._senders)} "
            f"最大尝试={self._max_attempts} 退避基数={self._backoff_seconds}s）"
        )
        while not stop.is_set():
            try:
                await self.process_due_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 单轮失败不影响后续
                print(f"[notify] 投递循环异常（忽略本轮）: {exc!r}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
