"""运维变更面（**唯一**一个）：手工终止卡死的生成任务。

为什么必须存在:
    卡在 ``running`` 的生成任务会让商品永久 409（前端按钮也因 ``status='generating'`` 被禁用），
    过去只能改数据库 —— 生产环境里这意味着「要人肉连库、且不留痕」。
    自动兜底有两条（``GenerationJobReaper`` 周期回收 + 触发生成时的**守卫宽容**），
    但它们都要等一个阈值；本接口给运维「立刻处置 + 写审计」的能力。

与 ops 只读面的关系（``routers/ops_router.py``）:
    ops 的读面**保持只读**（心跳/PEL/DLQ 仍然只呈现事实）；本模块是**唯一**的变更入口，
    且要求：必填原因、admin 独占、CAS 幂等、写 ``job_abort_audits``。

收回口径（与 result 消费器配合，晚到结果不会污染状态）:
    · job → ``failed`` + ``error``（含操作人与原因）；
    · 仅当 ``products.active_thread_id == job.thread_id`` 时：商品 → ``draft`` + 清空；
    · 已终态 → 幂等 no-op（返回 ``already_terminal=True``，不写审计、不改任何行）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.errors import ApiError
from ..models.orm import GenerationJob, Product
from ..repositories.job_abort_audits import JobAbortAuditRepo

__all__ = ["JobAdminService", "ABORTABLE_STATUSES"]

#: 可被手工终止的状态（终态不可终止：它们已经收口）
ABORTABLE_STATUSES = ("running", "waiting_input")


class JobAdminService:
    """生成任务的运维变更（当前只有「终止」）。"""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        """初始化。

        参数:
            session: DB 会话（一个请求一个）。
            org_id: 当前租户（来自 JWT claims）。
            session_factory: 预留（当前实现不用；保持与其它 service 一致的构造口径）。
        """
        self.session = session
        self.org_id = org_id
        self.audits = JobAbortAuditRepo(session, org_id)

    async def abort_job(
        self, job_id: uuid.UUID, *, actor_user_id: str, reason: str
    ) -> dict[str, Any]:
        """终止一个卡死/悬挂的生成任务并把商品放回可重试状态。

        参数:
            job_id: ``generation_jobs.id``。
            actor_user_id: 操作人（写审计）。
            reason: 终止原因（必填 —— 审计的意义就在这条原因上）。
        返回:
            ``{"job_id", "thread_id", "product_id", "job_status", "product_status",
              "product_released", "already_terminal"}``。
        异常:
            ApiError: 400（原因为空）、404（任务不存在/不属于当前租户）、409（任务已是终态）。
        幂等:
            已终态 → **409**（而不是 200）：终止一个已结束的任务是「操作没意义」，
            明确报错比静默成功更安全（避免运维误以为是自己终止生效了）。
        """
        if not reason or not reason.strip():
            raise ApiError(400, "终止任务必须提供 reason（审计要求可追溯）")
        clean_reason = reason.strip()

        job = (
            await self.session.execute(
                select(GenerationJob).where(
                    GenerationJob.id == job_id, GenerationJob.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if job is None:  # 越权与不存在同响应（不泄露存在性）
            raise ApiError(404, "资源不存在或不属于当前租户")
        if job.status not in ABORTABLE_STATUSES:
            raise ApiError(409, f"任务已是终态（status={job.status}），无需终止")

        product = (
            await self.session.execute(
                select(Product).where(Product.id == job.product_id, Product.org_id == job.org_id)
            )
        ).scalar_one_or_none()

        job.status = "failed"
        job.error = f"运维手工终止：{clean_reason}（by {actor_user_id[:8]}）"[:500]

        released = False
        if product is not None and product.active_thread_id == job.thread_id:
            product.status = "draft"
            product.active_thread_id = None
            released = True
        await self.session.flush()

        await self.audits.record(
            job_id=job.id,
            thread_id=job.thread_id,
            product_id=job.product_id,
            actor_user_id=actor_user_id,
            reason=clean_reason,
        )
        await self.session.commit()
        print(
            f"[ops-abort] job={job.id} thread={str(job.thread_id)[:8]} product={job.product_id} "
            f"释放商品={released} by={actor_user_id} reason={clean_reason!r}"
        )
        return {
            "job_id": str(job.id),
            "thread_id": str(job.thread_id),
            "product_id": str(job.product_id),
            "job_status": job.status,
            "product_status": product.status if product is not None else None,
            "product_released": released,
            "already_terminal": False,
        }
