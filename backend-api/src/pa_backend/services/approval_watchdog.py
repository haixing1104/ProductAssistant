"""审批补投守护：修复「已定案但 ai-engine 未收到 resume」的悬挂单。

故障场景（本守护存在的唯一理由）:
    ``decide`` 是「先 commit 审批结论，再 XADD job:approval」。若 XADD 那一刻 Redis 不可用，
    审批结论已落库、商品却停在 ``waiting_approval`` —— 前端看起来「审批了但没反应」。
    这里的守护会周期性找出这类悬挂单并补投。

判据（保守，宁可漏补不可误投）:
    ``approval.status ∈ (approved, rejected)``
    且 ``resolved_at`` 已过去 ``min_age_seconds``（给正常链路留出 resume 的时间）
    且 ``products.status = 'waiting_approval'``（商品还没被 resume 后的终态结果推进）

节流:
    每个待审单用 Redis 键 ``pa:{env}:approval:redrive:{id}``（SETNX + TTL）限频。
    没有节流的话，扫描间隔一到就会反复投递同一条消息（worker 反复 resume、锁竞争被放大）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.keys import RedisKeys
from ..core.redis_client import new_async_redis
from ..models.orm import HitlApproval, Product
from .ai_engine_client import AIEngineClient
from .approval_service import DECIDED_STATUSES, enqueue_resume

__all__ = ["ApprovalRedriveWatchdog", "DEFAULT_MIN_AGE_SECONDS", "DEFAULT_THROTTLE_SECONDS"]

#: 扫描间隔（秒）
SCAN_INTERVAL_SECONDS = 60.0
#: 定案后多久仍未推进才补投（给正常链路留时间，避免与正常 resume 抢投）
DEFAULT_MIN_AGE_SECONDS = 60
#: 同一单据的补投节流窗口（秒）
DEFAULT_THROTTLE_SECONDS = 3600


class ApprovalRedriveWatchdog:
    """周期性补投悬挂的审批 resume 消息。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        engine=None,
        min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
        throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
        scan_limit: int = 20,
    ) -> None:
        """初始化。

        参数:
            session_factory: 会话工厂。
            settings: 应用配置（读 env / redis_url）。
            engine: 投递客户端；None → 每个租户构造一个（``AIEngineClient`` 只持配置，构造廉价）。
            min_age_seconds: 定案后多久才允许补投。
            throttle_seconds: 同单的补投节流窗口。
            scan_limit: 单轮最多处理几条。
        """
        self._session_factory = session_factory
        self._settings = settings
        self._engine = engine
        self._min_age = max(1, min_age_seconds)
        self._throttle = max(1, throttle_seconds)
        self._scan_limit = max(1, scan_limit)
        self._keys = RedisKeys(settings.env)

    async def scan_once(self, *, now: datetime | None = None) -> int:
        """扫描一轮并补投。

        参数:
            now: 时间基准（默认 UTC now；测试可注入）。
        返回:
            实际补投的条数。
        """
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(seconds=self._min_age)
        enqueued = 0
        redis_client = new_async_redis(self._settings)
        try:
            async with self._session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(HitlApproval, Product)
                            .join(Product, HitlApproval.product_id == Product.id)
                            .where(
                                HitlApproval.status.in_(DECIDED_STATUSES),
                                HitlApproval.resolved_at.is_not(None),
                                HitlApproval.resolved_at <= cutoff,
                                Product.status == "waiting_approval",
                            )
                            .order_by(HitlApproval.resolved_at)
                            .limit(self._scan_limit)
                        )
                    )
                    .all()
                )
                for approval, _product in rows:
                    throttle_key = self._keys.approval_redrive(str(approval.id))
                    # SETNX + TTL：抢不到 = 最近已补投过 → 跳过
                    if not await redis_client.set(throttle_key, "1", ex=self._throttle, nx=True):
                        continue
                    engine = self._engine or AIEngineClient(self._settings)
                    if enqueue_resume(engine, approval):
                        enqueued += 1
                        print(
                            f"[watchdog] 补投 resume approval={approval.id} "
                            f"product={approval.product_id} status={approval.status}"
                        )
        finally:
            await redis_client.aclose()
        return enqueued

    async def run(self, stop: asyncio.Event, *, interval: float = SCAN_INTERVAL_SECONDS) -> None:
        """常驻循环（lifespan 启动）。

        参数:
            stop: 停止信号。
            interval: 扫描间隔（秒）。
        返回:
            无返回值。
        """
        print(
            f"[watchdog] env={self._settings.env} 审批补投守护启动"
            f"（最小年龄={self._min_age}s 节流={self._throttle}s）"
        )
        while not stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 单轮失败不影响后续
                print(f"[watchdog] 扫描异常（忽略本轮）: {exc!r}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
