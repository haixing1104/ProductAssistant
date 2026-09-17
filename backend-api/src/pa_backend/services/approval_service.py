"""审批服务：CAS 定案 / 投递 resume / 补投 / 深链定位。

为什么用 **DB 乐观锁 CAS** 而不是 Redis 分布式锁:
    审批的最终事实在 ``hitl_approvals`` 这一行上。用 ``UPDATE … WHERE status='pending'``
    让数据库自己充当唯一仲裁者：两个并发的批准请求，只有一个的 ``rowcount == 1``。
    若改用 Redis 锁，还要额外回答「锁丢了但已经改库了怎么办」这类问题 —— 多一层状态、多一类 bug。

投递时序（``decide`` 与 ``redrive`` 都是「先 commit 再 XADD」）:
    · 先 commit：审批结论是**事实**，必须落库可审计；ai-engine 的 resume 只是消费这个事实；
    · 投递失败**不回滚审批结论**（审批人点过了就是点过了），而是回传 ``resume_enqueued=false``
      交由补投守护 / 人工 redrive 处理 —— 与「purge 删行后投递失败不回滚」同一条原则：
      已完成的事实不能因下游抖动而回滚。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.errors import ApiError
from ..models.orm import HitlApproval, Product, SysUser
from .ai_engine_client import AIEngineClient

__all__ = ["ApprovalService", "DECIDED_STATUSES", "approver_names"]

#: 已定案（终态）的审批状态
DECIDED_STATUSES = ("approved", "rejected")

#: 审批状态 → ``job:approval`` 载荷里的取值（与 ai-engine ``_handle_approval`` 对齐）
RESULT_BY_STATUS = {"approved": "approved", "rejected": "rejected"}


async def approver_names(
    session: AsyncSession, *, org_id: str, approver_ids: list[uuid.UUID | None]
) -> dict[str, str]:
    """批量取审批人用户名（``approver_id`` → ``username``）。

    为什么 backend 要自己 join:
        ``sys_users`` 对 ai-engine **零权限**，审批链路上只有 ``approver_id``；
        而审批历史必须显示「谁批的」——只有 backend 能补全（见 README §2.6 第 4 条）。

    参数:
        session: DB 会话。
        org_id: 租户（第二道闸：审批行已按租户过滤）。
        approver_ids: 审批人 ID 列表（可含 None，调用方直接从行上取即可）。
    返回:
        ``{user_id: username}``；找不到的 ID 不出现在结果里（调用方回退展示「已停用用户」）。
    注意:
        写成**模块级函数**而不是只做实例方法：读路径（审批列表/详情）不需要构造
        ``ApprovalService``（它的构造会顺手建 Redis 投递客户端），省一次无谓的连接构造。
    """
    ids = [item for item in approver_ids if item is not None]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(SysUser.id, SysUser.username).where(SysUser.org_id == org_id, SysUser.id.in_(ids))
        )
    ).all()
    return {str(row[0]): row[1] for row in rows}


class ApprovalService:
    """待审单的读取、定案、恢复投递与补投。"""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        settings: Settings,
        engine: AIEngineClient | None = None,
    ) -> None:
        """初始化。

        参数:
            session: DB 会话。
            org_id: 当前租户（来自 JWT claims）。
            settings: 应用配置。
            engine: 投递客户端（测试可注入替身）。
        """
        self.session = session
        self.org_id = org_id
        self.settings = settings
        self.engine = engine if engine is not None else AIEngineClient(settings)

    # ------------------------------------------------------------------ 读
    async def list_approvals(
        self,
        *,
        status: str | None = None,
        product_id: uuid.UUID | None = None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[HitlApproval, Product]], int]:
        """列出审批单（含商品信息）+ 总数；可按状态与商品过滤。

        参数:
            status: ``pending`` / ``approved`` / ``rejected``；None = 全部状态。
            product_id: 只看某个商品的审批单（商品详情页「审批与驳回复盘」用它）。
            offset / limit: 分页。
        返回:
            ``([(approval, product), …], total)``；按 ``created_at`` 倒序（最新在前）。
        注意:
            为什么要有「按状态查」:
                原 ``list_pending`` 只回 ``status='pending'`` —— 审批人点完「批准/驳回」后，
                那张单就从界面上**彻底消失**了（既看不到自己批过什么，也看不到驳回原因），
                复盘只能去查库。审批历史是审批中心的基本诉求，不是可选项。
        """
        base = (
            select(HitlApproval, Product)
            .join(Product, HitlApproval.product_id == Product.id)
            .where(HitlApproval.org_id == self.org_id)
        )
        if status:
            base = base.where(HitlApproval.status == status)
        if product_id is not None:
            base = base.where(HitlApproval.product_id == product_id)
        total = int(
            (await self.session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        )
        rows = (
            await self.session.execute(
                base.order_by(HitlApproval.created_at.desc()).offset(max(0, offset)).limit(max(1, limit))
            )
        ).all()
        return [(row[0], row[1]) for row in rows], total

    async def list_pending(self, *, offset: int, limit: int) -> tuple[list[tuple[HitlApproval, Product]], int]:
        """列出待审单（等价于 ``list_approvals(status="pending")``；保留旧签名以兼容既有调用）。"""
        return await self.list_approvals(status="pending", offset=offset, limit=limit)

    async def approver_names(self, approver_ids: list[uuid.UUID | None]) -> dict[str, str]:
        """批量取审批人用户名（实例方法形式，委托给模块级 ``approver_names``）。"""
        return await approver_names(self.session, org_id=self.org_id, approver_ids=approver_ids)

    async def get_or_404(self, approval_id: uuid.UUID) -> HitlApproval:
        """取待审单；不存在或不属于本租户 → 404。"""
        approval = (
            await self.session.execute(
                select(HitlApproval).where(HitlApproval.id == approval_id, HitlApproval.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if approval is None:
            raise ApiError(404, "资源不存在或不属于当前租户")
        return approval

    # ------------------------------------------------------------------ 写
    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approve: bool,
        feedback: str | None,
        approver_id: str,
    ) -> dict:
        """审批定案（CAS）并投递 ``job:approval``。

        参数:
            approval_id: 待审单 ID。
            approve: True = 批准，False = 驳回。
            feedback: 审批意见（驳回时作为 Reflection 重写的输入）。
            approver_id: 审批人（写入 ``approver_id`` 列）。
        返回:
            ``{"approval_id", "status", "thread_id", "resume_enqueued"}``。
        异常:
            ApiError: 404（不存在/越权）、409（已被他人定案）。
        注意:
            **不校验商品当前状态**：审批人手上的单子可能对应一条已被新线程取代的线程，
            resume 旧线程是无害的（ai-engine 会把它收口为终态）；
            而「因为商品状态看起来不对就拒绝审批人的决定」反而会把流程卡死。
        """
        target_status = "approved" if approve else "rejected"
        result = await self.session.execute(
            update(HitlApproval)
            .where(
                HitlApproval.id == approval_id,
                HitlApproval.org_id == self.org_id,
                HitlApproval.status == "pending",  # ← CAS：只有仍待审的行能被改
            )
            .values(
                status=target_status,
                feedback=feedback,
                approver_id=uuid.UUID(approver_id),
                resolved_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount == 0:
            await self.session.rollback()
            existing = (
                await self.session.execute(
                    select(HitlApproval).where(
                        HitlApproval.id == approval_id, HitlApproval.org_id == self.org_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise ApiError(404, "资源不存在或不属于当前租户")
            raise ApiError(409, f"该审批单已被处理（当前状态：{existing.status}）")

        approval = (
            await self.session.execute(select(HitlApproval).where(HitlApproval.id == approval_id))
        ).scalar_one()
        await self.session.commit()

        enqueued = self._enqueue_resume(approval, result=RESULT_BY_STATUS[target_status])
        return {
            "approval_id": str(approval.id),
            "status": approval.status,
            "thread_id": str(approval.thread_id),
            "resume_enqueued": enqueued,
        }

    async def redrive(self, approval_id: uuid.UUID) -> dict:
        """补投：对「已定案」的单子重新投递 ``job:approval``（人工兜底）。

        参数:
            approval_id: 待审单 ID。
        返回:
            ``{"approval_id", "status", "resume_enqueued"}``。
        异常:
            ApiError: 404（不存在/越权）、409（仍在待审 —— 应先审批而不是补投）。
        """
        approval = await self.get_or_404(approval_id)
        if approval.status not in DECIDED_STATUSES:
            raise ApiError(409, "该审批单仍在待审，请先审批")
        enqueued = self._enqueue_resume(approval, result=RESULT_BY_STATUS[approval.status])
        return {"approval_id": str(approval.id), "status": approval.status, "resume_enqueued": enqueued}

    def _enqueue_resume(self, approval: HitlApproval, *, result: str) -> bool:
        """投递 ``job:approval``（薄封装：统一走模块级 ``enqueue_resume``）。"""
        return enqueue_resume(self.engine, approval, result=result)


def enqueue_resume(engine: AIEngineClient, approval: HitlApproval, *, result: str | None = None) -> bool:
    """投递 ``job:approval``（``decide`` / ``redrive`` / 补投守护的**唯一**投递入口）。

    参数:
        engine: 投递客户端。
        approval: 审批单（读 thread_id/product_id/org_id/feedback/status）。
        result: 覆盖投递结果（缺省按 ``approval.status`` 推导；仅待审单推导失败返回 False）。
    返回:
        是否投递成功（失败只记日志：审批结论已落库，交给补投处理）。
    注意:
        载荷必须带 ``product_id``/``org_id``：ai-engine resume 后用它们把终态结果发回
        ``result:workflow``（缺了这两个字段，resume 之后的终态就无主了）。
    """
    effective = result or RESULT_BY_STATUS.get(approval.status)
    if effective is None:
        print(f"[approval] 单据 {approval.id} 状态 {approval.status!r} 无对应 resume 结果，跳过投递")
        return False
    try:
        engine.resume_approval(
            thread_id=str(approval.thread_id),
            product_id=str(approval.product_id),
            org_id=str(approval.org_id),
            result=effective,
            feedback=approval.feedback,
        )
        return True
    except Exception as exc:  # noqa: BLE001 投递失败不改审批结论，交由补投/redrive 兜底
        print(f"[approval] resume 投递失败 approval={approval.id}（可补投）: {exc!r}")
        return False
