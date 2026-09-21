"""审批中心路由：列表（待审 / 已处理）/ 批准 / 驳回 / 补投 / 深链定位。

权限:
    · 列表与审批：``admin`` 或 ``reviewer``（operator **不能审批** —— 生成与把关分离，
      这是 HITL 的意义所在）；
    · 补投：``admin``（属运维动作）；
    · 深链定位：**匿名可访问**，但只返回「跳到哪张单」的定位信息（票据本身不含审批权限，
      真正审批仍要登录态 + RBAC）。

为什么审批列表要做「摘要 + 详情」两级:
    列表页一次可能有 N 条待办，若每条都带完整图文快照（几十 KB），列表接口会明显变慢；
    列表返回 ``snapshot_summary``，详情页（带 ``?with_snapshot=1``）再取完整快照。

列表为什么还要有「已处理」:
    只回 ``pending`` 的话，审批人点完按钮那张单就**从界面上消失了** —— 批了什么、
    驳回原因是什么，全都无处可查；商品详情页的「驳回复盘」也依赖同一条能力。
    因此列表按 ``status`` 过滤（默认 ``pending``，保持既有调用方行为不变），
    并提供 ``status=all`` 显式表达「全部状态（含已定案）」：详情页复盘必须看到驳回单，
    而缺省语义是 pending —— 这正是 2026-09「驳回后详情页看不到内容」的根因。

连接信息（``approver_name`` / ``notifications``）为什么由 backend 补:
    原因：``sys_users`` 对 ai-engine 零权限、``notification_outbox`` 是 backend 域数据，
    审批人姓名与「通知发出去没有」只有在 backend 侧才能 join 出来。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import security
from ..core.deps import CurrentUser, get_db, require_roles
from ..schemas.api import ApprovalDecisionRequest, serialize_approval
from ..services.approval_service import ApprovalService, approver_names
from ..repositories.approval_audits import overrides_by_approval, redrive_summary_by_approval
from ..services.notifications import notifications_by_approval
from .common import ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/approvals", tags=["approvals"])

#: 可审批的角色（operator 不在此列，见模块 docstring）
APPROVER_ROLES = ("admin", "reviewer")

#: 允许的审批状态过滤值（具体状态；缺省语义见 ``list_approvals``）
APPROVAL_STATUSES = ("pending", "approved", "rejected")

#: 「全部状态」哨兵值：显式表达「不过滤」。
#: 为什么需要它（2026-09 实测事故）:
#:     缺省（不传 status）等价 ``pending`` 是既有兼容约定，于是商品详情页的
#:     「审批与驳回复盘」按 product_id 查历史时**永远查不到已定案的单** ——
#:     界面表现为「该商品还没有审批记录」，而库里其实有驳回单。
#:     调用方必须能显式说"我要全部"，且不能靠空串（``?status=`` 仍是 400，语义不含糊）。
ALL_STATUS = "all"


def _service(request: Request, session: AsyncSession, user: CurrentUser) -> ApprovalService:
    """构造审批服务（注入 app 级 settings）。"""
    return ApprovalService(session, user.org_id, settings=request.app.state.settings)


def _name_lookup_org(user: CurrentUser) -> str | None:
    """审批人名字查询的租户口径。

    为什么要区分（2026-09）:
        平台超管**跨租户**审批时，``hitl_approvals.approver_id`` 是他本人（归属平台组织），
        而他当前的「租户」是目标租户 X —— 若仍按 X 过滤 ``sys_users``，「谁批的」会显示为空。
        超管 → ``None``（只按 ID 查，见 ``approval_service.approver_names`` 的说明）。
    安全:
        非超管仍严格按 ``user.org_id`` 过滤；且名字查询的 ID 只来自**已按租户过滤**的审批行。
    """
    return None if user.is_superuser else user.org_id


async def _decorate(
    session: AsyncSession,
    user: CurrentUser,
    rows: list[tuple],
    *,
    include_snapshot: bool,
) -> list[dict]:
    """把 (approval, product) 行加工成响应项：审批人姓名 + 通知投递状态。

    参数:
        session: DB 会话（两个批量查询复用同一会话，避免 N+1）。
        user: 当前身份（取 ``org_id``）。
        rows: ``ApprovalService.list_approvals`` 的结果。
        include_snapshot: 是否带完整图文快照。
    返回:
        响应项列表（与 ``serialize_approval`` 一致，另加 ``approver_name`` 与 ``notifications``）。
    """
    approvals = [row[0] for row in rows]
    approval_ids = [item.id for item in approvals]
    names = await _service_names(session, user, approvals)
    notify_map = await notifications_by_approval(session, org_id=user.org_id, approval_ids=approval_ids)
    # 审计回显：补投痕迹（几次/何时/结果）与「人工放行命中点」记录 —— 两者都是审批可信度的一部分
    redrive_map = await redrive_summary_by_approval(session, org_id=user.org_id, approval_ids=approval_ids)
    override_map = await overrides_by_approval(session, org_id=user.org_id, approval_ids=approval_ids)
    out: list[dict] = []
    for approval, product in rows:
        item = serialize_approval(approval, product, include_snapshot=include_snapshot)
        item["approver_name"] = names.get(str(approval.approver_id)) if approval.approver_id else None
        item["notifications"] = notify_map.get(str(approval.id), [])
        item["redrive"] = redrive_map.get(str(approval.id))
        item["override"] = override_map.get(str(approval.id))
        out.append(item)
    return out


async def _service_names(session: AsyncSession, user: CurrentUser, approvals: list) -> dict[str, str]:
    """批量取审批人姓名（只读路径，不构造 ApprovalService，避免多余地建 Redis 客户端）。"""
    if not approvals:
        return {}
    return await approver_names(
        session, org_id=_name_lookup_org(user), approver_ids=[item.approver_id for item in approvals]
    )


@router.get("")
async def list_approvals(
    request: Request,
    response: Response,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="pending（默认）/ approved / rejected / all（全部，含已定案）；空串非法",
    ),
    product_id: str | None = Query(default=None, description="只看某个商品的审批单（驳回复盘用）"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    with_snapshot: bool = Query(default=False, description="是否带完整图文快照（详情页用）"),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*APPROVER_ROLES)),
) -> dict:
    """审批单列表（默认待审；可查已处理、某商品的历史、以及 ``status=all`` 全量）。"""
    if status_filter is not None and status_filter not in APPROVAL_STATUSES + (ALL_STATUS,):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status 只能是 {'/'.join(APPROVAL_STATUSES)} 或 {ALL_STATUS}",
        )
    effective_status: str | None = status_filter if status_filter is not None else "pending"
    if effective_status == ALL_STATUS:
        effective_status = None  # 哨兵 → 不过滤状态（详情页复盘要看到已定案单）
    rows, total = await _service(request, session, user).list_approvals(
        status=effective_status,
        product_id=to_uuid(product_id) if product_id else None,
        offset=offset,
        limit=limit,
    )
    response.headers["X-Total-Count"] = str(total)
    return ok(await _decorate(session, user, rows, include_snapshot=with_snapshot))


@router.get("/pending")
async def list_pending(
    request: Request,
    response: Response,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    with_snapshot: bool = Query(default=False, description="是否带完整图文快照（详情页用）"),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*APPROVER_ROLES)),
) -> dict:
    """待办列表（等价于 ``GET /approvals?status=pending``；保留既有契约）。"""
    rows, total = await _service(request, session, user).list_pending(offset=offset, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return ok(await _decorate(session, user, rows, include_snapshot=with_snapshot))


@router.get("/deeplink")
async def resolve_deeplink(
    request: Request,
    ticket: str = Query(description="通知里的深链票据（kind=approval_redirect）"),
) -> dict:
    """解析深链票据 → 返回定位信息（匿名可用；**不授予任何审批权限**）。

    参数:
        ticket: 票据（由 ``services/notifications/message.py`` 生成）。
    返回:
        ``{"approval_id", "product_id", "org_id", "valid": True}``。
    异常:
        HTTPException: 401（票据无效/过期/类型不符）。
    """
    settings = request.app.state.settings
    try:
        claims = security.decode_token(ticket, settings)
    except Exception as exc:  # noqa: BLE001 过期/签名不符统一 401
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="审批链接无效或已过期") from exc
    if claims.get("kind") != security.KIND_APPROVAL_REDIRECT:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="票据类型不符")
    return ok(
        {
            "valid": True,
            "approval_id": claims.get("approval_id"),
            "product_id": claims.get("product_id"),
            "org_id": claims.get("org_id"),
        }
    )


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*APPROVER_ROLES)),
) -> dict:
    """批准：CAS 定案 + 投递 ``job:approval``（``resume_enqueued=false`` 时走补投）。"""
    service = _service(request, session, user)
    result = await service.decide(
        to_uuid(approval_id), approve=True, feedback=body.feedback, approver_id=user.id
    )
    return ok(result)


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*APPROVER_ROLES)),
) -> dict:
    """驳回：CAS 定案 + 投递 ``job:approval``（反馈会作为 Reflection 重写的输入）。"""
    service = _service(request, session, user)
    result = await service.decide(
        to_uuid(approval_id), approve=False, feedback=body.feedback, approver_id=user.id
    )
    return ok(result)


@router.post("/{approval_id}/redrive")
async def redrive(
    approval_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """补投：对已定案但 ai-engine 未收到的单子重新投递（运维兜底）。

    返回带 ``needed`` / ``outcome``（enqueued / not_needed / throttled / enqueue_failed）：
    "引擎已消费过该结论"时**不再假装成功**（实测：对已跑完的线程再 resume 是静默 no-op）。
    """
    service = _service(request, session, user)
    return ok(await service.redrive(to_uuid(approval_id), actor_user_id=user.id))


@router.get("/{approval_id}")
async def get_approval(
    approval_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*APPROVER_ROLES)),
) -> dict:
    """审批单详情（含完整图文快照 + 审批人姓名 + 通知投递状态）。"""
    service = _service(request, session, user)
    approval = await service.get_or_404(to_uuid(approval_id))
    product = await _load_product(session, approval.product_id, user.org_id)
    item = serialize_approval(approval, product)
    names = await approver_names(
        session, org_id=_name_lookup_org(user), approver_ids=[approval.approver_id]
    )
    notify_map = await notifications_by_approval(
        session, org_id=user.org_id, approval_ids=[approval.id]
    )
    item["approver_name"] = names.get(str(approval.approver_id)) if approval.approver_id else None
    item["notifications"] = notify_map.get(str(approval.id), [])
    # 审计回显（与列表同一形状）：补投痕迹 + 人工放行记录
    redrive_map = await redrive_summary_by_approval(session, org_id=user.org_id, approval_ids=[approval.id])
    override_map = await overrides_by_approval(session, org_id=user.org_id, approval_ids=[approval.id])
    item["redrive"] = redrive_map.get(str(approval.id))
    item["override"] = override_map.get(str(approval.id))
    return ok(item)


async def _load_product(session: AsyncSession, product_id, org_id: str):
    """取商品（不存在返回 None：彻底删除后审批单可能还在，详情仍应可用）。"""
    from ..repositories.products import ProductRepo

    return await ProductRepo(session, org_id).get(product_id)
