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
from ..core.keys import RedisKeys
from ..core.redis_client import new_async_redis
from ..models.orm import HitlApproval, Product, SysUser
from ..repositories.approval_audits import ApprovalAuditRepo
from .ai_engine_client import AIEngineClient

__all__ = ["ApprovalService", "DECIDED_STATUSES", "approver_names"]

#: 已定案（终态）的审批状态
DECIDED_STATUSES = ("approved", "rejected")

#: 审批状态 → ``job:approval`` 载荷里的取值（与 ai-engine ``_handle_approval`` 对齐）
RESULT_BY_STATUS = {"approved": "approved", "rejected": "rejected"}

#: 人工补投的节流窗口（秒）——与补投守护（approval_watchdog.DEFAULT_THROTTLE_SECONDS）同值：
#: 同一张单在窗口内反复补投没有意义（投递失败会立刻反映在 outcome 上，成功则无需重投）。
REDRIVE_THROTTLE_SECONDS = 3600


def snapshot_violations(snapshot: dict | None) -> list:
    """取审批快照里的「评估命中点」（``evaluation_result.violations``）。

    为什么单独抽函数（而不是内联）:
        W6 的"放行必须写理由"与"放行留痕"两处判断必须**完全同源**，否则会出现
        "后端认为有命中点、审计却记 0 条"这类自相矛盾的记录（实测快照的
        ``evaluation_result`` 只含 passed/score/violations/facts_checked 四个键，
        不含 ``errors`` —— 以 violations 为准）。

    参数:
        snapshot: ``hitl_approvals.content_snapshot``（可能为 None / 老数据缺失该键）。
    返回:
        命中点列表（每项为 dict：keyword/reason/rule_id/severity）；无命中返回 []。
    """
    evaluation = (snapshot or {}).get("evaluation_result") or {}
    violations = evaluation.get("violations")
    return list(violations) if isinstance(violations, list) else []


async def approver_names(
    session: AsyncSession, *, org_id: str, approver_ids: list[uuid.UUID | None]
) -> dict[str, str]:
    """批量取审批人用户名（``approver_id`` → ``username``）。

    为什么 backend 要自己 join:
        ``sys_users`` 对 ai-engine **零权限**，审批链路上只有 ``approver_id``；
        而审批历史必须显示「谁批的」——只有 backend 能补全。

    参数:
        session: DB 会话。
        org_id: 租户（过滤依据；**平台超管场景可传 None**，见下）。
        approver_ids: 审批人 ID 列表（可含 None，调用方直接从行上取即可）。
    返回:
        ``{user_id: username}``；找不到的 ID 不出现在结果里（调用方回退展示「已停用用户」）。
    注意:
        写成**模块级函数**而不是只做实例方法：读路径（审批列表/详情）不需要构造
        ``ApprovalService``（它的构造会顺手建 Redis 投递客户端），省一次无谓的连接构造。
    ``org_id=None`` 的用途（平台超管，2026-09）:
        超管切换到租户 X 审批后，``approver_id`` 是他自己（归属平台组织）—— 若仍按
        ``org_id=X`` 过滤，审批历史里「谁批的」会变成空。传 None = 只按 ID（不按租户）查。
        安全前提：``approver_ids`` **只能来自调用方已按租户过滤的审批行**
        （审批列表/详情都先过了 org 过滤），因此这里没有「凭 ID 猜别人租户用户」的入口。
    """
    ids = [item for item in approver_ids if item is not None]
    if not ids:
        return {}
    stmt = select(SysUser.id, SysUser.username).where(SysUser.id.in_(ids))
    if org_id is not None:
        stmt = stmt.where(SysUser.org_id == org_id)
    rows = (await session.execute(stmt)).all()
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
        # 放行前置检查（W6）：批准一张「带评估命中点」的单子必须说明理由 ——
        # 合规命中转人工后，审批人一点就上架且不留痕，是 2026-09 实测的合规缺口。
        # 注意顺序：本检查在 CAS 之前（此时行仍是 pending，不会白改一行再回滚）。
        current = await self.get_or_404(approval_id)
        violations = snapshot_violations(current.content_snapshot)
        if approve and violations and not feedback:
            raise ApiError(
                422,
                f"该内容存在 {len(violations)} 条评估命中点（合规/事实风险），放行必须填写理由："
                "理由会写入审批审计（approval_overrides），供事后追溯。",
            )
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
            await self.session.execute(
                select(HitlApproval)
                .where(HitlApproval.id == approval_id)
                # 必须 populate_existing：上面的放行前置检查已经把这行读进 Session 的身份映射，
                # 之后的 CAS UPDATE 不会自动刷新该内存对象 —— 不加这一句会读回 status='pending'
                # 的旧值（实测踩过：接口回 200 但 status 仍旧，前端误判"审批没生效"）。
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        # 放行留痕（W6）：与审批定案**同事务**写入，避免"批准成功但审计没落库"
        if approve and violations:
            await ApprovalAuditRepo(self.session, self.org_id).record_override(
                approval_id=approval.id,
                thread_id=approval.thread_id,
                actor_user_id=approver_id,
                violations=violations,
                reason=feedback or "",
            )
        await self.session.commit()

        enqueued = self._enqueue_resume(approval, result=RESULT_BY_STATUS[target_status])
        return {
            "approval_id": str(approval.id),
            "status": approval.status,
            "thread_id": str(approval.thread_id),
            "resume_enqueued": enqueued,
            "override": bool(approve and violations),
        }

    async def redrive(self, approval_id: uuid.UUID, *, actor_user_id: str | None = None) -> dict:
        """补投：把「已定案但 ai-engine 未收到」的单子重新投递 ``job:approval``（人工兜底）。

        语义（2026-09 实测后收紧）:
            只有「引擎很可能没收到」时才真的投递 —— 判据与补投守护一致：
            ``products.status == 'waiting_approval'``（商品还没被 resume 后的终态结果推进）。
            商品已推进（published / draft）说明引擎已经消费过该结论，此时再 resume 是
            **静默 no-op**（实测：不报错、不重跑、不重复落库、不翻转决策），
            因此返回 ``not_needed`` 而不是假装"补投成功"。

        参数:
            approval_id: 待审单 ID。
            actor_user_id: 操作者（写审计；守护自动补投时为 None）。
        返回:
            ``{"approval_id", "status", "needed", "outcome", "reason", "resume_enqueued"}``；
            ``outcome ∈ enqueued / not_needed / throttled / enqueue_failed``。
        异常:
            ApiError: 404（不存在/越权）、409（仍在待审 —— 应先审批而不是补投）。
        """
        approval = await self.get_or_404(approval_id)
        if approval.status not in DECIDED_STATUSES:
            raise ApiError(409, "该审批单仍在待审，请先审批")

        repo = ApprovalAuditRepo(self.session, self.org_id)
        needed, reason = await self._redrive_needed(approval)
        if not needed:
            await repo.record_redrive(
                approval_id=approval.id,
                thread_id=approval.thread_id,
                outcome="not_needed",
                actor_user_id=actor_user_id,
                reason=reason,
            )
            await self.session.commit()
            return {
                "approval_id": str(approval.id),
                "status": approval.status,
                "needed": False,
                "outcome": "not_needed",
                "reason": reason,
                "resume_enqueued": False,
            }

        if not await self._acquire_redrive_slot(str(approval.id)):
            reason = "节流窗口内已补投过（同一张单不重复投递）"
            await repo.record_redrive(
                approval_id=approval.id,
                thread_id=approval.thread_id,
                outcome="throttled",
                actor_user_id=actor_user_id,
                reason=reason,
            )
            await self.session.commit()
            return {
                "approval_id": str(approval.id),
                "status": approval.status,
                "needed": True,
                "outcome": "throttled",
                "reason": reason,
                "resume_enqueued": False,
            }

        enqueued = self._enqueue_resume(approval, result=RESULT_BY_STATUS[approval.status])
        await repo.record_redrive(
            approval_id=approval.id,
            thread_id=approval.thread_id,
            outcome="enqueued" if enqueued else "enqueue_failed",
            actor_user_id=actor_user_id,
            reason=reason,
        )
        await self.session.commit()
        return {
            "approval_id": str(approval.id),
            "status": approval.status,
            "needed": True,
            "outcome": "enqueued" if enqueued else "enqueue_failed",
            "reason": reason,
            "resume_enqueued": enqueued,
        }

    async def _redrive_needed(self, approval: HitlApproval) -> tuple[bool, str]:
        """是否需要真的补投：判据与 ``approval_watchdog`` 完全一致（保守，宁可漏补不可误投）。

        参数:
            approval: 已定案的审批单。
        返回:
            ``(needed, reason)``：reason 是给人看的一句话（前端直接展示）。
        """
        product = (
            await self.session.execute(
                select(Product).where(Product.id == approval.product_id, Product.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if product is None:
            return False, "商品已不存在（无需补投）"
        if product.status == "waiting_approval":
            return True, "商品仍停在待审批：引擎很可能未收到该结论"
        return False, f"商品已推进到 {product.status}：引擎已消费该结论，补投是无副作用的空操作"

    async def _acquire_redrive_slot(self, approval_id: str) -> bool:
        """补投节流（SETNX + TTL）：同一张单在窗口内只允许投一次。

        为什么必须节流: 投递失败/重复点击会在 ``job:approval`` 上堆消息，worker 反复 resume、
        线程锁竞争被放大（补投守护的同一取舍，见 ``approval_watchdog`` 模块 docstring）。

        参数:
            approval_id: 审批单 ID。
        返回:
            True = 抢到窗口（可以投递）；False = 窗口内已投过。
        注意:
            Redis 不可用时**放行投递**（宁可真投，也不因为限流组件故障让卡住的单据彻底无法补投）。
        """
        try:
            client = new_async_redis(self.settings)
        except Exception:  # noqa: BLE001 构造失败同样按"放行"处理
            return True
        try:
            key = RedisKeys(self.settings.env).approval_redrive(approval_id)
            return bool(await client.set(key, "1", nx=True, ex=REDRIVE_THROTTLE_SECONDS))
        except Exception:  # noqa: BLE001 Redis 抖动不阻断人工运维动作
            return True
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

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
