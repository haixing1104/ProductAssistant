"""运维路由（P6/P8）：消费心跳 / 流与消费组 / 死信队列（只读）+ 手工终止卡死任务（唯一变更面）。

为什么读面保持只读（不提供「重投 / 清空」这类按钮）:
    运维面板最容易被误用成「一键修复」——而 DLQ 里的消息之所以进 DLQ，是因为**重试到上限仍失败**
    （商品数据异常、外部依赖不可用等）。盲目重投只会再造一条毒消息，还会掩盖根因。
    因此读面只呈现事实，处置属人工操作（判断根因后重投或修数据）。

唯一的例外：``POST /ops/jobs/{job_id}/abort``（P8 新增）
    卡在 ``running`` 的生成任务会让商品**永久 409**（前端按钮也因 ``status='generating'`` 被禁用），
    而商品状态由 backend 独占管理 —— 只读面让运维「看得见却动不了」，过去只能人肉改库且不留痕。
    因此开一个**受控**变更面：必填原因、admin 独占、已终态一律 409（不做静默 no-op）、
    并写 ``job_abort_audits``（只增不改的审计表，0005）。
    自动兜底另有两条：``GenerationJobReaper``（周期回收）与触发生成时的守卫宽容。

权限：``admin`` 独占（键名、消息内容属内部运维信息；且运营岗看这些也没有可执行的动作）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..services.job_admin_service import JobAdminService
from ..services.ops_reader import DLQ_MAX_ENTRIES, OpsReader
from .common import ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/ops", tags=["ops"])


class AbortJobBody(BaseModel):
    """终止任务的请求体。"""

    # 必填：审计的价值全在这条原因上（「为什么它该被终止」事后必须能复盘）
    reason: str = Field(min_length=1, max_length=200, description="终止原因（必填，写审计）")


@router.get("/overview")
async def overview(
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """一屏看全：worker 心跳（含 ``stalled`` 判定）/ 契约流与消费组 PEL / DLQ 概况 / 卡住任务。

    ``stuck_jobs`` 的判据与 ``GenerationJobReaper`` 同源（同一常量），因此面板里看到的就是
    「reaper 迟早会回收」的那批；要**立刻**处置用下面的 abort 接口（面板上有按钮）。
    """
    reader = OpsReader(request.app.state.settings, session=session, org_id=user.org_id)
    try:
        return ok(await reader.overview())
    finally:
        await reader.close()


@router.get("/dlq")
async def dlq_entries(
    request: Request,
    domain: str = Query(description="死信域，如 job:generate（对应键 pa:{env}:dlq:job:generate）"),
    limit: int = Query(default=20, ge=1, le=DLQ_MAX_ENTRIES),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """回看某个死信流的最新消息（只读；处置属人工操作，见本模块顶部说明）。"""
    reader = OpsReader(request.app.state.settings)
    try:
        return ok(await reader.dlq_entries(domain, limit=limit))
    finally:
        await reader.close()


@router.post("/jobs/{job_id}/abort")
async def abort_job(
    job_id: str,
    body: AbortJobBody,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """终止一个卡死/悬挂的生成任务：job→failed、商品→draft（可重新生成），并写审计。

    用途：运维面板「卡住任务」表里点「终止」，立刻解开「商品永久 409 / 按钮不可点」的死锁。
    自动兜底（reaper / 守卫宽容）需要等阈值，本接口用于**立刻处置**并留下「谁/为何」的记录。
    幂等口径：已终态的任务 → 409（不静默成功，避免运维误以为是自己终止生效的）。
    """
    service = JobAdminService(session, user.org_id)
    return ok(await service.abort_job(to_uuid(job_id), actor_user_id=user.id, reason=body.reason))