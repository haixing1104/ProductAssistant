"""商品仓储：本租户商品主数据的读写。

状态机（``database/sql/0001_schema.sql`` 的 CHECK）:
    ``draft → generating → waiting_approval → published``，另有 ``archived`` / ``deleted``。
    本模块负责**触发即推进**（generating），终态由 ``workflow_result_consumer`` 推进（P4）。

删除语义（**只有一种**，别去找软删）:
    ``hard_delete``：物理删行（彻底删除），必须**先删子表**（``generation_jobs`` / ``hitl_approvals``
    对 ``products.id`` 有物理外键，且未定义级联），再删本行。
    曾经设计过 ``soft_delete``（``status='deleted'``），现已**按业务决定移除**：
    删除动作在 UI/API 上都只有「彻底删除」一条路径，避免「删了但数据还在」的语义混乱。
    DB CHECK 里的 ``deleted`` / ``archived`` 保留，仅用于承载**历史数据**。

为什么 ``raw_images`` 由本模块维护（而不是 ai-engine）:
    ai-engine 只读商品（``role_pa_ai`` 仅 SELECT），上传图的 URL 列表是 backend 域数据；
    ai-engine 的 ``node_image`` 会把它当作「有上传图」的判据（规范形状 = 公有 URL 字符串数组）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import GenerationJob, HitlApproval, Product
from .base import OrgScopedRepo

__all__ = ["ProductRepo", "is_sku_conflict"]

#: 默认列表要排除的状态（``deleted`` 是**历史数据**专用：软删已下线，新数据不会再进入该状态；
#: 显式 ``?status=deleted`` 时仍可查，便于发现与清理历史行）
HIDDEN_IN_LIST = ("deleted",)


def is_sku_conflict(error: IntegrityError) -> bool:
    """判断异常是否为「组织内 SKU 重复」（``uq_products_org_sku``）。"""
    return "uq_products_org_sku" in str(getattr(error, "orig", error))


class ProductRepo(OrgScopedRepo):
    """本租户的商品仓储。"""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        """初始化（见 ``OrgScopedRepo``）。"""
        super().__init__(session, org_id)

    # ------------------------------------------------------------------ 读
    async def list_products(
        self, *, offset: int, limit: int, status: str | None = None, ids: list[uuid.UUID] | None = None
    ) -> tuple[list[Product], int]:
        """分页列出商品（默认排除软删；可按状态过滤）。

        参数:
            offset: 偏移量。
            limit: 每页条数。
            status: 指定状态（如 ``draft``）；None = 全部（排除 ``HIDDEN_IN_LIST``）。
            ids: 限定商品 ID 集合（P4 消费器/测试可用）。
        返回:
            ``(items, total)``；总数用于 ``X-Total-Count``。
        """
        stmt = self._scoped(select(Product), Product)
        if status:
            stmt = stmt.where(Product.status == status)
        else:
            stmt = stmt.where(Product.status.notin_(HIDDEN_IN_LIST))
        if ids is not None:
            stmt = stmt.where(Product.id.in_(ids))
        total = int(
            (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(Product.created_at.desc()).offset(max(0, offset)).limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def get(self, product_id: uuid.UUID) -> Product | None:
        """按 id 取本租户商品（越权与不存在同返回 None）。"""
        stmt = self._scoped(select(Product), Product).where(Product.id == product_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------ 写
    async def create(
        self,
        *,
        sku_code: str,
        title: str,
        base_price: Decimal,
        stock_status: str,
        owner_id: uuid.UUID | None,
        raw_images: list[str],
    ) -> Product:
        """新建商品（``(org_id, sku_code)`` 冲突时抛 ``IntegrityError``，由服务层转 409）。"""
        product = Product(
            org_id=self.org_id,
            sku_code=sku_code,
            title=title,
            base_price=base_price,
            stock_status=stock_status,
            status="draft",
            owner_id=owner_id,
            raw_images=raw_images,
        )
        self.session.add(product)
        await self.session.flush()
        return product

    async def hard_delete(self, product: Product) -> None:
        """物理删除商品行（**先清子表**，见模块 docstring）。

        注意:
            调用方负责在**本函数之后** commit，并在 commit 成功后再投递 ``job:product_purge``
            （ai-engine 的 purge 守卫要求「行已不存在」，见 ``services/ai_engine_client`` docstring）。
        """
        await self.session.execute(
            GenerationJob.__table__.delete().where(
                GenerationJob.org_id == self.org_id, GenerationJob.product_id == product.id
            )
        )
        await self.session.execute(
            HitlApproval.__table__.delete().where(
                HitlApproval.org_id == self.org_id, HitlApproval.product_id == product.id
            )
        )
        await self.session.delete(product)
        await self.session.flush()

    async def set_raw_images(self, product: Product, urls: list[str]) -> Product:
        """覆盖写入上传图 URL 列表（规范形状 = 公有 URL 字符串数组）。"""
        product.raw_images = list(urls)
        await self.session.flush()
        return product
