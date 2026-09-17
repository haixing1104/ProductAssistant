"""运维路由（P6）：只读的消费心跳 / 流与消费组 / 死信队列。

为什么只读（不提供「重投 / 清空」这类按钮）:
    运维面板最容易被误用成「一键修复」——而 DLQ 里的消息之所以进 DLQ，是因为**重试到上限仍失败**
    （商品数据异常、外部依赖不可用等）。盲目重投只会再造一条毒消息，还会掩盖根因。
    因此这里只呈现事实，处置走 README 里的 SOP（人工判断根因后重投或修数据）。

权限：``admin`` 独占（键名、消息内容属内部运维信息；且运营岗看这些也没有可执行的动作）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..core.deps import CurrentUser, require_roles
from ..services.ops_reader import DLQ_MAX_ENTRIES, OpsReader
from .common import ok

__all__ = ["router"]

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/overview")
async def overview(
    request: Request,
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """一屏看全：worker 心跳（含 ``stalled`` 判定）/ 契约流长度与消费组 PEL / DLQ 概况。"""
    reader = OpsReader(request.app.state.settings)
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
    """回看某个死信流的最新消息（只读；处置 SOP 见 backend-api/README）。"""
    reader = OpsReader(request.app.state.settings)
    try:
        return ok(await reader.dlq_entries(domain, limit=limit))
    finally:
        await reader.close()