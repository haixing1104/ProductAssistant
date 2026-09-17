"""评估轨迹路由（只读 ``schema_pa_ai.evaluation_logs``）——「AI 思考轨迹」面板数据源。

展示口径:
    每条记录对应一次评估（``evaluator_type=rule|llm``）：规则命中的 ``rule_id`` + ``errors`` 明细、
    LLM 的 ``score`` 与 ``latency_ms``。Reflection 重试会在同一 ``thread_id`` 下累积多条 ——
    前端按时间倒序展示，即可还原「重写了两次、每次为什么不通过」。

权限: 与内容版本一致，backend 只 SELECT（写归 ai-engine）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..repositories.audits import EvaluationsRepo
from ..repositories.products import ProductRepo
from ..schemas.api import serialize_eval_log
from .common import NOT_FOUND_DETAIL, ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/products", tags=["evaluations"])

READ_ROLES = ("admin", "operator", "reviewer")


@router.get("/{product_id}/evaluation-logs")
async def list_evaluation_logs(
    product_id: str,
    limit: int = Query(default=50, ge=1, le=200, description="返回条数上限（新→旧）"),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """列出某商品的评估日志（新→旧）。"""
    pid = to_uuid(product_id)
    if await ProductRepo(session, user.org_id).get(pid) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    logs = await EvaluationsRepo(session, user.org_id).list_logs(pid, limit=limit)
    return ok([serialize_eval_log(item) for item in logs])
