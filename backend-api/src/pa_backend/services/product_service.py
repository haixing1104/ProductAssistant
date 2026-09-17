"""商品业务服务：CRUD / 彻底删除 / CSV 导入 / 触发生成。

事务与投递的时序（本模块最容易出错的地方，改动前请先读）:
    ``trigger_generation`` 与 ``purge`` 都是「先 commit 再 XADD」：
        · purge：ai-engine 的 ``_handle_purge`` 有守卫 —— 商品行仍存在就**中止清理并 ack 丢弃**。
          先投递后提交 = 那条消息永久丢失（行删了、AI 侧数据留着）；
        · generate：worker 要读商品素材、结果消费器要按 ``thread_id`` 找 job 行。
          行/任务不存在时消息会被判为「商品缺失」草率收口。
    投递失败（Redis 不可用）时**回滚状态**：商品不能永远卡在 ``generating``
    ——那会让运营既看不到结果也点不动「重新生成」（按钮通常按状态禁用）。

删除只有一种语义（见 ``repositories/products.py`` 模块 docstring）:
    ``delete`` 即**物理删除**（审计 + 删行 + 级联清理）——不再提供软删/恢复。

依赖注入:
    ``engine``（Redis 投递）与 ``oss``（对象清理）都可注入；测试用小替身替换，
    从而在不依赖 Redis/OSS 的情况下验证状态机与审计语义。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.errors import ApiError
from ..models.orm import GenerationJob, Product
from ..repositories.audits import AuditRepo
from ..repositories.compliance import ComplianceRepo
from ..repositories.products import ProductRepo
from .ai_engine_client import AIEngineClient, new_thread_id
from .csv_import import parse_products_csv
from .generation_job_reaper import is_stale, terminalize
from .oss import OssStorage, build_oss_storage

__all__ = ["ProductService"]

#: 历史 ``deleted`` / ``archived`` 行不可再被操作（触发生成、上传图、修改）
#: 说明：软删功能已下线（删除=物理删除），这两个状态只可能是**历史数据**；
#: 保留写保护是为了「历史行被当成正常商品误改」这种情况不会发生。
BLOCKED_STATUSES = ("deleted", "archived")


class ProductService:
    """商品域服务（一个请求一个实例；所有查询都以 ``org_id`` 过滤）。"""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        settings: Settings,
        engine: AIEngineClient | None = None,
        oss: OssStorage | None = None,
    ) -> None:
        """初始化。

        参数:
            session: DB 会话。
            org_id: 当前租户（来自 JWT claims）。
            settings: 应用配置（构造默认 engine/oss，或提供测试替身）。
            engine: 投递客户端；None → 用 settings 构造（懒建连接）。
            oss: 对象存储；None → 由 settings 构造（未配置则为 None，清理自动跳过）。
        """
        self.session = session
        self.org_id = org_id
        self.settings = settings
        self.repo = ProductRepo(session, org_id)
        self.audits = AuditRepo(session, org_id)
        self.compliance = ComplianceRepo(session)
        self.engine = engine if engine is not None else AIEngineClient(settings)
        self.oss = oss if oss is not None else build_oss_storage(settings)

    # ------------------------------------------------------------------ 读
    async def list_products(
        self, *, offset: int, limit: int, status: str | None = None
    ) -> tuple[list[Product], int]:
        """分页列出商品（默认排除软删）。"""
        return await self.repo.list_products(offset=offset, limit=limit, status=status)

    async def get_or_404(self, product_id: uuid.UUID) -> Product:
        """取商品；不存在或不属于本租户 → 404（与越权同响应）。"""
        product = await self.repo.get(product_id)
        if product is None:
            raise ApiError(404, "资源不存在或不属于当前租户")
        return product

    async def active_job(self, product: Product) -> GenerationJob | None:
        """取该商品当前进行中的任务（``active_thread_id`` 指向的 job 且状态非终态）。"""
        if product.active_thread_id is None:
            return None
        stmt = select(GenerationJob).where(
            GenerationJob.org_id == self.org_id,
            GenerationJob.thread_id == product.active_thread_id,
            GenerationJob.status.in_(("running", "waiting_input")),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------ 写
    async def create(
        self,
        *,
        sku_code: str,
        title: str,
        base_price: Decimal,
        stock_status: str,
        owner_id: str,
        raw_images: list[str] | None = None,
    ) -> Product:
        """新建商品（``draft``；SKU 组织内唯一冲突由数据库拒绝并转 409）。"""
        from sqlalchemy.exc import IntegrityError

        from ..repositories.products import is_sku_conflict

        try:
            product = await self.repo.create(
                sku_code=sku_code,
                title=title,
                base_price=base_price,
                stock_status=stock_status,
                owner_id=uuid.UUID(owner_id),
                raw_images=raw_images or [],
            )
        except IntegrityError as exc:
            await self.session.rollback()
            if is_sku_conflict(exc):
                raise ApiError(409, f"该 SKU 已存在：{sku_code}") from exc
            raise
        await self.session.commit()
        return product

    async def update(self, product_id: uuid.UUID, fields: dict) -> Product:
        """部分更新商品（仅允许白名单字段；``deleted`` 商品不可改）。

        参数:
            product_id: 商品 ID。
            fields: 允许的键：``title`` / ``base_price`` / ``stock_status`` / ``raw_images``。
        返回:
            更新后的商品。
        注意:
            ``raw_images`` 会被**整体覆盖**（不做增量合并）：上传/删除图片的操作端持有完整列表，
            合并语义反而会让「删掉一张图」变得无法表达。
        """
        product = await self.get_or_404(product_id)
        if product.status in BLOCKED_STATUSES:
            raise ApiError(409, "已删除或归档的商品不可修改")
        if "title" in fields and fields["title"] is not None:
            product.title = fields["title"]
        if "base_price" in fields and fields["base_price"] is not None:
            product.base_price = fields["base_price"]
        if "stock_status" in fields and fields["stock_status"] is not None:
            product.stock_status = fields["stock_status"]
        if "raw_images" in fields and fields["raw_images"] is not None:
            product.raw_images = list(fields["raw_images"])
        await self.session.commit()
        return product

    async def import_csv(self, raw: bytes) -> dict:
        """CSV 同步导入（**要么全进要么全不进**，见 ``csv_import`` 模块 docstring）。

        参数:
            raw: 上传文件字节。
        返回:
            ``{"created": n, "skus": [...]}``。
        异常:
            CsvImportError: 文件格式/内容不合格（由路由层转 400 并附逐行报告）。
            ApiError(409): 与库中已有 SKU 冲突（附冲突清单，同样不导入任何数据）。
        """
        from sqlalchemy.exc import IntegrityError

        from ..repositories.products import is_sku_conflict

        rows = parse_products_csv(raw)  # 不合格会抛 CsvImportError（不在此吞掉）
        existing = set(
            (
                await self.session.execute(
                    select(Product.sku_code).where(
                        Product.org_id == self.org_id, Product.sku_code.in_([r.sku_code for r in rows])
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            raise ApiError(409, f"{len(existing)} 个 SKU 已存在（本次未导入任何数据）：{', '.join(sorted(existing))}")
        try:
            for row in rows:
                await self.repo.create(
                    sku_code=row.sku_code,
                    title=row.title,
                    base_price=row.base_price,
                    stock_status=row.stock_status,
                    owner_id=None,
                    raw_images=row.raw_images,
                )
        except IntegrityError as exc:
            await self.session.rollback()
            if is_sku_conflict(exc):
                raise ApiError(409, "导入过程中检测到 SKU 冲突（本次未导入任何数据）") from exc
            raise
        await self.session.commit()
        return {"created": len(rows), "skus": [row.sku_code for row in rows]}

    async def trigger_generation(self, product_id: uuid.UUID) -> GenerationJob:
        """触发生成：登记 job + 推进商品状态 + 投递 ``job:generate``。

        参数:
            product_id: 商品 ID。
        返回:
            新建的 ``GenerationJob``（``running``）。
        异常:
            ApiError: 404（不存在/越权）、409（商品已删除/归档、已有**仍在处理中**的任务）、
                503（投递失败 —— 此时状态已回滚为 ``draft``，可安全重试）。
        步骤（顺序即语义，见模块 docstring）:
            ① 商品必须可作业（非 deleted/archived），且没有进行中的任务（防重复扣费）；
               例外的**守卫宽容**：进行中的任务若已超期僵死（``job_stale_minutes``），
               先就地回收（job→failed、商品→draft）再继续 —— 否则卡死的任务会让商品永久 409；
            ② 生成新 ``thread_id``（幂等锚点）→ 建 job(running) + 商品 ``generating`` + 绑定线程；
            ③ **入队瞬间固化 rules 快照**（含时效过滤；ai-engine 侧不做时间判断）；
            ④ commit 后再投递；投递失败则回滚状态并报 503。
        """
        product = await self.get_or_404(product_id)
        if product.status in BLOCKED_STATUSES:
            raise ApiError(409, "已删除或归档的商品不能触发生成")
        running = await self.active_job(product)
        if running is not None:
            # **守卫宽容**：只有「仍在正常处理窗口内」的任务才拦。
            # 卡死的 job（worker 被杀/消息丢失/图挂起）会让该商品**永久** 409，而前端按钮也按
            # status 禁用 → 用户既点不动也无法重试，只能改库（2026-09 实测踩到）。
            # 因此对「已确认僵死」的任务就地回收（job→failed、商品→draft），继续本次触发。
            if is_stale(running, stale_minutes=self.settings.job_stale_minutes):
                reaped = await terminalize(self.session, running)
                print(
                    f"[backend] 守卫宽容：回收僵死任务 thread={reaped['thread_id'][:8]} "
                    f"product={reaped['product_id']}（超期 > {self.settings.job_stale_minutes}min），放行本次触发"
                )
                await self.session.flush()
            else:
                raise ApiError(409, f"该商品已有进行中的生成任务（thread_id={running.thread_id}）")

        thread_id = uuid.UUID(new_thread_id())
        job = GenerationJob(
            thread_id=thread_id, org_id=uuid.UUID(self.org_id), product_id=product.id, status="running"
        )
        self.session.add(job)
        product.active_thread_id = thread_id
        product.status = "generating"
        await self.session.flush()
        rules = await self.compliance.snapshot()
        await self.session.commit()

        try:
            self.engine.trigger_generation(
                thread_id=str(thread_id), product_id=str(product.id), org_id=self.org_id, rules=rules
            )
        except Exception as exc:  # noqa: BLE001 投递失败必须回滚状态，否则商品永远卡在 generating
            job.status = "failed"
            job.error = f"任务投递失败：{type(exc).__name__}"
            product.status = "draft"
            product.active_thread_id = None
            await self.session.commit()
            raise ApiError(503, "任务投递失败，请稍后重试") from exc
        return job

    async def purge(self, product_id: uuid.UUID, *, actor_user_id: str, reason: str) -> dict:
        """彻底删除（审计 + 物理删行 + 清理自有对象 + 通知 ai-engine 清理）。

        参数:
            product_id: 商品 ID。
            actor_user_id: 操作人（写审计）。
            reason: 删除原因（必填，审计要求可追溯）。
        返回:
            ``{"product_id", "sku_code", "raw_images_removed", "purge_enqueued"}``。
        异常:
            ApiError: 404（不存在/越权）、503（消息投递失败 —— 此时状态已回滚，数据未被删）。
        时序（见模块 docstring）:
            ① 读商品并记录 ``raw_images``（**删行前**，否则 URL 列表就丢了）；
            ② 同事务：写审计 + 物理删行（含子表）；
            ③ commit；
            ④ XADD ``job:product_purge``（失败只记日志：行已删，AI 侧清理可人工补投，
               但**不能回滚**已完成的删除 —— 审计已经落库）；
            ⑤ best-effort 删除自有上传件对象。
        """
        if not reason or not reason.strip():
            raise ApiError(400, "彻底删除必须提供 reason（审计要求）")
        product = await self.get_or_404(product_id)
        raw_images = [url for url in (product.raw_images or []) if isinstance(url, str)]
        sku_code = product.sku_code
        await self.audits.record(
            product_id=product.id,
            sku_code=sku_code,
            title=product.title,
            actor_user_id=actor_user_id,
            reason=reason.strip(),
        )
        await self.repo.hard_delete(product)
        await self.session.commit()

        purge_enqueued = True
        try:
            self.engine.trigger_product_purge(product_id=str(product_id), org_id=self.org_id)
        except Exception as exc:  # noqa: BLE001 行已删除，清理消息失败不能回滚删除
            purge_enqueued = False
            print(f"[backend] purge 消息投递失败（商品行已删除，可人工补投）: {exc!r}")

        removed = 0
        if self.oss is not None and raw_images:
            try:
                removed = self.oss.delete_urls(raw_images)
            except Exception as exc:  # noqa: BLE001 对象清理 best-effort
                print(f"[backend] 上传件清理失败（忽略）: {exc!r}")
        return {
            "product_id": str(product_id),
            "sku_code": sku_code,
            "raw_images_removed": removed,
            "purge_enqueued": purge_enqueued,
        }

