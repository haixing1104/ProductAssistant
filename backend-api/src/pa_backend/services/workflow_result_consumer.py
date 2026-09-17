"""``result:workflow`` 消费器：把 ai-engine 的图终态结果推进成商品/任务状态。

映射表（``result`` → ``products.status`` × ``generation_jobs.status``）::

    published       → published          × succeeded       + 清空 active_thread_id
    awaiting_human  → waiting_approval   × waiting_input   + 建 hitl_approvals(pending)（含 content_snapshot）
                                                            + 写 notification_outbox
    rejected        → draft              × failed          + 清空 active_thread_id
    failed          → draft              × failed          + 清空 active_thread_id

五条 PA 专属加固（相对 ProductPilot，逐条都有明确的事故场景）:
    ① **历史 deleted 商品跳过**（``products.status='deleted'``）：删除后晚到的结果不得复活状态。
       注：软删功能已下线（删除=物理删行，行不存在走上面的「商品已被彻底删除」分支），
       本守卫现在防的是**历史数据**（老库里 status='deleted' 的行、以及备份恢复的数据）；
    ② **等待审批期只认审批终态**（``job.status='waiting_input'`` 时仅接受 ``published``/``rejected``）：
       否则重复/乱序的 ``awaiting_human`` 会把已批准的商品又打回待审批；
    ③ **旧线程晚到**（``thread_id ≠ products.active_thread_id``）：只终态化旧 job，不动当前商品 ——
       用户「点两次生成」时两条线程都会回结果，晚到的那条不能覆盖新线程的状态；
    ④ **已终态幂等**（job 已 succeeded/failed）：直接跳过，避免重复写库与重复建审批单；
    ⑤ **未知 result 丢弃**：版本升级引入的新取值不应把消费器打成毒消息循环。

消费位置与可靠性:
    单进程 ``XREAD`` + 本地游标（``0-0`` 起补账）—— backend 单实例消费；重启后的重复投递由
    数据库幂等守卫（第 ④ 条）兜底，无需消费组/PEL 状态。这与 ai-engine 的 PEL/DLQ 机制互补：
    那边保证「消息不丢」，这边保证「重复不写坏数据」。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.keys import RedisKeys
from ..core.redis_client import new_async_redis
from ..models.orm import GenerationJob, HitlApproval, Product
from .event_envelope import check_version, decode_fields
from .notifications.writer import enqueue_pending_notifications

__all__ = ["RESULT_TO_STATUS", "WorkflowResultConsumer"]

#: 结果取值 → (商品状态, 任务状态)
RESULT_TO_STATUS: dict[str, tuple[str, str]] = {
    "published": ("published", "succeeded"),
    "awaiting_human": ("waiting_approval", "waiting_input"),
    "rejected": ("draft", "failed"),
    "failed": ("draft", "failed"),
}

#: 任务终态（幂等判据）
TERMINAL_JOB_STATUSES = ("succeeded", "failed")

#: 消费循环参数
_READ_COUNT = 20
_BLOCK_MS = 1000
_RETRY_BACKOFF_SECONDS = 1.0


class WorkflowResultConsumer:
    """消费 ``pa:{env}:result:workflow``，按幂等守卫推进状态机。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        notify_channels: tuple[str, ...] = (),
    ) -> None:
        """初始化。

        参数:
            session_factory: 会话工厂（每条消息一个独立事务，避免长事务持锁）。
            settings: 应用配置（读 ``env`` / ``redis_url``）。
            notify_channels: 需要投递的外部通知渠道（如 ``("dingtalk",)``）；
                空元组 = 只建审批单不出站（本地/测试默认）。
        """
        self._session_factory = session_factory
        self._settings = settings
        self._keys = RedisKeys(settings.env)
        self._notify_channels = tuple(notify_channels)

    # ------------------------------------------------------------ 单条处理
    async def process_payload(self, payload: dict[str, Any]) -> bool:
        """应用一条结果消息（独立事务）。

        参数:
            payload: 解码后的载荷（``thread_id`` / ``product_id`` / ``org_id`` / ``result`` /
                可选 ``content_snapshot``）。
        返回:
            True = 已处理或**有意跳过**（调用方应推进游标）。
        异常:
            Exception: 非预期错误上抛（调用方不推进游标 → 下轮重试该条）。
        """
        warning = check_version(payload)
        if warning:
            print(f"[result-consumer] ⚠ {warning}")

        result = payload.get("result")
        if result not in RESULT_TO_STATUS:
            print(f"[result-consumer] 未知 result={result!r}，已丢弃")
            return True
        try:
            thread_id = uuid.UUID(str(payload["thread_id"]))
            product_id = uuid.UUID(str(payload["product_id"]))
        except (KeyError, ValueError) as exc:
            print(f"[result-consumer] 载荷缺少关键字段或格式非法，已丢弃: {exc!r}")
            return True

        product_status, job_status = RESULT_TO_STATUS[result]
        async with self._session_factory() as session:
            job = (
                await session.execute(select(GenerationJob).where(GenerationJob.thread_id == thread_id))
            ).scalar_one_or_none()
            if job is None:
                return True  # 消息不属于当前库（reset 残留 / 别的环境）→ 跳过
            if job.status in TERMINAL_JOB_STATUSES:
                return True  # ④ 已终态：幂等跳过
            if job.status == "waiting_input" and result not in ("published", "rejected"):
                return True  # ② 等待审批期只认审批终态（重复的 awaiting_human 忽略）

            product = (
                await session.execute(
                    select(Product).where(Product.id == product_id, Product.org_id == job.org_id)
                )
            ).scalar_one_or_none()

            if product is None:
                # 商品已被彻底删除：只把任务收口，不再推进商品
                job.status = job_status
                await session.commit()
                return True
            if product.status == "deleted":
                return True  # ① 历史 deleted 行：整条结果跳过（不得复活成 published）

            job.status = job_status
            if product.active_thread_id == thread_id:
                product.status = product_status
                if product_status in ("published", "draft"):
                    product.active_thread_id = None
            else:
                # ③ 旧线程晚到：只终态化该 job，不改当前商品
                print(
                    f"[result-consumer] 旧线程结果 thread={thread_id} 与当前 "
                    f"active_thread_id={product.active_thread_id} 不一致：仅收口任务"
                )

            if result == "awaiting_human":
                await self._create_pending_approval(
                    session, job=job, product=product, payload=payload, thread_id=thread_id
                )
            await session.commit()
        return True

    async def _create_pending_approval(
        self,
        session: AsyncSession,
        *,
        job: GenerationJob,
        product: Product,
        payload: dict[str, Any],
        thread_id: uuid.UUID,
    ) -> None:
        """为转人工的线程建档 ``hitl_approvals(pending)`` 并写通知 outbox（同线程幂等）。

        参数:
            session: 当前事务会话（与状态推进**同事务**：保证「待审批商品必有对应待办单」）。
            job: 对应任务。
            product: 对应商品。
            payload: 结果载荷（读 ``content_snapshot``）。
            thread_id: 线程 ID（幂等键）。
        """
        existing = (
            await session.execute(select(HitlApproval).where(HitlApproval.thread_id == thread_id))
        ).scalar_one_or_none()
        if existing is not None:
            return  # 幂等：重复的 awaiting_human 不再建单
        approval = HitlApproval(
            org_id=product.org_id,
            product_id=product.id,
            thread_id=thread_id,
            status="pending",
            channel="web",
            content_snapshot=payload.get("content_snapshot"),
        )
        session.add(approval)
        await session.flush()
        if self._notify_channels:
            await enqueue_pending_notifications(
                session,
                org_id=str(product.org_id),
                approval=approval,
                product=product,
                settings=self._settings,
                channels=self._notify_channels,
            )
        print(
            f"[result-consumer] 已建待审单 product={product.id} thread={thread_id} "
            f"job={job.id} 通知渠道={list(self._notify_channels)}"
        )

    # ------------------------------------------------------------ 常驻循环
    async def run(self, stop: asyncio.Event) -> None:
        """常驻消费循环（lifespan 启动）：先补账历史（``0-0``），再持续尾随。

        参数:
            stop: 停止信号（``set()`` 后循环退出）。
        返回:
            无返回值。
        注意:
            单轮异常只退避重试，**不让进程退出** —— 消费器挂掉等于「生成完成但状态永不更新」，
            是比崩溃更隐蔽的故障。
        """
        stream = self._keys.workflow_result()
        client = new_async_redis(self._settings)
        cursor = "0-0"
        print(f"[result-consumer] env={self._settings.env} 开始消费 {stream}（从 0-0 补账）")
        try:
            while not stop.is_set():
                try:
                    response = await client.xread({stream: cursor}, count=_READ_COUNT, block=_BLOCK_MS)
                    if not response:
                        continue
                    for _stream_name, messages in response:
                        for message_id, fields in messages:
                            await self.process_payload(decode_fields(fields))
                            cursor = message_id
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 单轮失败退避，守护进程不退出
                    print(f"[result-consumer] 消费失败（{_RETRY_BACKOFF_SECONDS}s 后重试）: {exc!r}")
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        finally:
            await client.aclose()
