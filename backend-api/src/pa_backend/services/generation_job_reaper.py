"""生成任务僵死回收（reaper）：把「永远 running 的 job」终态化，解开商品的死锁。

故障场景（本模块存在的唯一理由）:
    ``trigger_generation`` 的守卫是「该商品已有进行中的生成任务 → 409」，前端也据此把按钮禁用
    （``canGenerate = status !== 'generating' && status !== 'waiting_approval'``）。
    一旦 job 卡在 ``running``（worker 被杀、消息丢失、图挂起），商品就永久停在 ``generating``：
    用户点不动、后端 409 —— **只能改数据库**（2026-09 实测踩到：任务 18:45 创建、无终态，
    商品一直 generating）。这里周期性把「超期未推进」的任务判死并放行商品。

判据（保守，宁可晚收不可误杀）:
    ``generation_jobs.status ∈ (running, waiting_input)``
    且 ``updated_at`` 已过去 ``stale_minutes``（默认 15 分钟，远大于实测 <1min 的正常生成耗时）。

为什么不用 Redis 线程锁 ``pa:{env}:lock:{thread_id}`` 判死:
    该键是 **worker ↔ worker 的内部互斥**，契约明确「backend 不得读写」（``core/keys.py``
    故意不暴露它，且有测试钉住）。跨进程去猜 worker 的内部状态会破坏这条红线；
    而 DB 的 ``updated_at`` 完全在本模块的权限与语义范围内，且足够保守。

收回口径（与 ``WorkflowResultConsumer`` 的两道守卫配合，晚到的结果不会污染状态）:
    · job → ``failed`` + ``error`` 写明原因；
    · **仅当** ``products.active_thread_id == job.thread_id`` 时：商品 → ``draft`` + 清空 active_thread_id；
    · 已终态的任务一律跳过（幂等）。晚到的结果会被消费器的「已终态幂等跳过」丢弃
      （见 ``workflow_result_consumer`` 守卫 ③④）。

``updated_at`` 的赋值口径:
    本表有 ``trg_generation_jobs_updated``（BEFORE UPDATE → ``now()``）触发器，因此这里**不手写**
    该列（写了也会被覆盖）—— 判据读它、DB 维护它，两边不重叠。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..models.orm import GenerationJob, Product

__all__ = [
    "GenerationJobReaper",
    "STALE_CANDIDATE_STATUSES",
    "STALE_JOB_ERROR",
    "DEFAULT_STALE_MINUTES",
    "is_stale",
    "terminalize",
]

#: 可被判死的任务状态（终态 succeeded/failed 永不进入）
STALE_CANDIDATE_STATUSES = ("running", "waiting_input")

#: 判死阈值（分钟）：远大于正常生成耗时（实测 45s 量级），宁可晚收也不误杀
DEFAULT_STALE_MINUTES = 15

#: 扫描间隔（秒）
DEFAULT_SCAN_INTERVAL_SECONDS = 60.0

#: 单轮最多回收几条（防一轮把库扫爆）
DEFAULT_SCAN_LIMIT = 50

#: 写入 ``generation_jobs.error`` 的原因文案（前端「最近一次任务失败原因」直接展示它）
STALE_JOB_ERROR = "生成任务超时未推进（疑似 worker 中断/消息丢失），已自动回收，可重新生成"


def _as_utc(moment: datetime) -> datetime:
    """把时间统一成 aware-UTC（DB 是 timestamptz，但测试/手工构造可能是 naive）。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def is_stale(job: GenerationJob, *, stale_minutes: int, now: datetime | None = None) -> bool:
    """判断任务是否已僵死（超期未推进）。

    参数:
        job: 任务 ORM 实例（读 ``status`` / ``updated_at``）。
        stale_minutes: 判死阈值（分钟）。
        now: 时间基准（默认 UTC now；测试注入）。
    返回:
        True = 已僵死，可安全回收；False = 仍在正常处理窗口内（**必须**继续拦住重复触发）。
    """
    if job.status not in STALE_CANDIDATE_STATUSES:
        return False
    moment = _as_utc(now or datetime.now(timezone.utc))
    updated = job.updated_at
    if updated is None:  # 历史脏数据：缺时间戳按僵死处理（否则永远解不开）
        return True
    cutoff = moment - timedelta(minutes=max(1, stale_minutes))
    return _as_utc(updated) <= cutoff


async def terminalize(
    session: AsyncSession,
    job: GenerationJob,
    *,
    reason: str = STALE_JOB_ERROR,
) -> dict[str, Any]:
    """把一条僵死任务终态化，并在必要时把商品放回可重试状态。

    参数:
        session: DB 会话（**本函数不 commit**，由调用方决定事务边界）。
        job: 待回收的任务（``status`` 必须是 ``STALE_CANDIDATE_STATUSES`` 之一）。
        reason: 写入 ``job.error`` 的原因文案。
    返回:
        ``{"job_id", "thread_id", "product_id", "product_released"}``；
        ``product_released=False`` 表示商品已被更新的线程接管（**不动它**）。
    """
    job.status = "failed"
    job.error = reason[:500]

    product = (
        await session.execute(
            select(Product).where(Product.id == job.product_id, Product.org_id == job.org_id)
        )
    ).scalar_one_or_none()

    released = False
    # 只有「商品当前指向的就是这条线程」才回收商品状态：否则说明用户已重新触发过，
    # 新线程才是权威的（晚到的旧线程不得打扰它 —— 与 result 消费器守卫 ③ 同一口径）。
    if product is not None and product.active_thread_id == job.thread_id:
        product.status = "draft"
        product.active_thread_id = None
        released = True
    await session.flush()
    return {
        "job_id": str(job.id),
        "thread_id": str(job.thread_id),
        "product_id": str(job.product_id),
        "product_released": released,
    }


class GenerationJobReaper:
    """周期性回收僵死的生成任务（lifespan 内启动，与 ``ApprovalRedriveWatchdog`` 同一形态）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        stale_minutes: int | None = None,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
        scan_interval: float | None = None,
    ) -> None:
        """初始化。

        参数:
            session_factory: 会话工厂。
            settings: 应用配置（读 ``job_stale_minutes`` / ``job_reaper_interval_seconds``）。
            stale_minutes: 判死阈值（分钟）；None → 用配置值。
            scan_limit: 单轮最多回收几条。
            scan_interval: 扫描间隔（秒）；None → 用配置值。
        """
        self._session_factory = session_factory
        self._settings = settings
        self._stale_minutes = max(1, stale_minutes if stale_minutes is not None else settings.job_stale_minutes)
        self._scan_limit = max(1, scan_limit)
        self._interval = float(
            scan_interval
            if scan_interval is not None
            else max(1, settings.job_reaper_interval_seconds)
        )

    @property
    def stale_minutes(self) -> int:
        """当前判死阈值（分钟）—— 供启动横幅与测试断言。"""
        return self._stale_minutes

    async def scan_once(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """扫描一轮并回收。

        参数:
            now: 时间基准（默认 UTC now；测试注入）。
        返回:
            本轮回收的任务摘要列表（空列表 = 没有僵死任务）。
        """
        moment = _as_utc(now or datetime.now(timezone.utc))
        cutoff = moment - timedelta(minutes=self._stale_minutes)
        reaped: list[dict[str, Any]] = []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(GenerationJob)
                        .where(
                            GenerationJob.status.in_(STALE_CANDIDATE_STATUSES),
                            GenerationJob.updated_at <= cutoff,
                        )
                        .order_by(GenerationJob.updated_at)
                        .limit(self._scan_limit)
                    )
                )
                .scalars()
                .all()
            )
            for job in rows:
                summary = await terminalize(session, job)
                reaped.append(summary)
                print(
                    f"[reaper] 回收僵死任务 job={summary['job_id']} thread={summary['thread_id'][:8]} "
                    f"product={summary['product_id']} 释放商品={summary['product_released']} "
                    f"（超期 > {self._stale_minutes}min）"
                )
            if reaped:
                await session.commit()
        return reaped

    async def run(self, stop: asyncio.Event, *, interval: float | None = None) -> None:
        """常驻循环（lifespan 启动）。

        参数:
            stop: 停止信号。
            interval: 扫描间隔（秒）；None → 用配置值。
        返回:
            无返回值。
        """
        period = float(interval if interval is not None else self._interval)
        print(
            f"[reaper] env={self._settings.env} 僵死任务回收启动"
            f"（阈值={self._stale_minutes}min 间隔={period:g}s 单轮上限={self._scan_limit}）"
        )
        while not stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 单轮失败不影响后续（与 watchdog 同一取舍）
                print(f"[reaper] 扫描异常（忽略本轮）: {exc!r}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                continue
