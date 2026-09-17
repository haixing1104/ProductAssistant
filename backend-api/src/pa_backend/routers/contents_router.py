"""内容版本路由（只读 ``schema_pa_ai.product_contents``）。

归属边界（``database/sql/0002_roles_grants.sql``）:
    写归 ai-engine；backend 只有 SELECT。因此本文件**只有 GET**，不提供任何写端点 ——
    这是「谁负责业务，谁拥有写入权」的机器化表达（越权写入会被数据库拒绝）。

为什么挂在 ``/products/{id}/contents`` 下:
    版本天然从属于商品；同时复用 ``to_uuid`` 的 404 语义（id 非法 = 不存在 = 越权）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..repositories.audits import ContentsRepo
from ..repositories.products import ProductRepo
from ..schemas.api import serialize_content
from .common import NOT_FOUND_DETAIL, ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/products", tags=["contents"])

READ_ROLES = ("admin", "operator", "reviewer")


async def _ensure_visible(session: AsyncSession, org_id: str, product_id) -> None:
    """确认商品属于本租户（越权/不存在一律 404；不泄露实体是否存在）。"""
    from fastapi import HTTPException, status

    if await ProductRepo(session, org_id).get(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


@router.get("/{product_id}/contents")
async def list_contents(
    product_id: str,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """列出某商品的全部内容版本（新→旧；无版本返回空数组，属正常冷启动）。"""
    pid = to_uuid(product_id)
    await _ensure_visible(session, user.org_id, pid)
    versions = await ContentsRepo(session, user.org_id).list_versions(pid)
    return ok([serialize_content(item) for item in versions])


@router.get("/{product_id}/contents/latest")
async def latest_content(
    product_id: str,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """最新版本（无版本返回 ``null`` —— 前端据此显示「尚未生成」空态）。"""
    pid = to_uuid(product_id)
    await _ensure_visible(session, user.org_id, pid)
    content = await ContentsRepo(session, user.org_id).latest(pid)
    return ok(serialize_content(content) if content is not None else None)
