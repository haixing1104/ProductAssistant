"""商品路由：列表 / 详情 / 新建 / 更新 / 彻底删除 / CSV 导入 / 触发生成。

权限模型（写操作矩阵）:
    · 读（列表/详情/内容/轨迹）：登录即可（三种角色都能看）；
    · 写（新建/更新/导入/触发生成）：``admin`` 或 ``operator``；
    · 危险写（**彻底删除**）：``admin`` —— purge 是物理删除（审计 + 删行 + 级联清理），
      必须收口到管理员。**删除只有这一条路径**：软删已下线（见 ``delete_product_not_supported``）。

分页: ``offset``/``limit`` 查询参数；总数走 ``X-Total-Count`` 响应头（响应体保持数组）。

路由顺序: ``/import-csv`` 与 ``/{product_id}/...`` 形状不同，本无冲突；
但仍按「静态路径在前」排列，避免将来新增其它 ``/{product_id}`` 语义时踩坑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from ..core.deps import CurrentUser, get_db, require_roles
from ..core.errors import ApiError, envelope
from ..schemas.api import ProductCreateRequest, ProductUpdateRequest, PurgeRequest, serialize_product
from ..services.csv_import import CsvImportError
from ..services.product_service import ProductService
from .common import ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/products", tags=["products"])

#: 读接口允许的角色（三种都能读）
READ_ROLES = ("admin", "operator", "reviewer")
#: 常规写操作允许的角色
WRITE_ROLES = ("admin", "operator")


def _service(request: Request, session: AsyncSession, user: CurrentUser) -> ProductService:
    """构造商品服务（注入 app 级 settings；Redis 投递客户端由服务内部懒建）。"""
    return ProductService(session, user.org_id, settings=request.app.state.settings)


@router.get("")
async def list_products(
    request: Request,
    response: Response,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status", description="按状态过滤（如 draft）"),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """列出本租户商品（默认排除软删；``status=deleted`` 可显式查询软删商品）。"""
    service = _service(request, session, user)
    items, total = await service.list_products(offset=offset, limit=limit, status=status_filter)
    response.headers["X-Total-Count"] = str(total)
    return ok([serialize_product(item) for item in items])


@router.post("")
async def create_product(
    body: ProductCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """新建商品（``draft``；SKU 组织内唯一 → 冲突 409）。"""
    service = _service(request, session, user)
    product = await service.create(
        sku_code=body.sku_code,
        title=body.title,
        base_price=body.base_price,
        stock_status=body.stock_status,
        owner_id=user.id,
        raw_images=body.raw_images,
    )
    return ok(serialize_product(product))


@router.post("/import-csv")
async def import_csv(
    request: Request,
    file: UploadFile = File(description="CSV 文件（UTF-8；表头 sku_code,title,base_price,stock_status）"),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """CSV 同步导入（**要么全进要么全不进**；不合格行随 400 一并返回，便于修完重传）。"""
    raw = await file.read()
    service = _service(request, session, user)
    try:
        result = await service.import_csv(raw)
    except CsvImportError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=envelope({"row_errors": exc.row_errors}, status.HTTP_400_BAD_REQUEST, exc.message),
        )
    return ok(result)


@router.get("/{product_id}")
async def get_product(
    product_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """商品详情（附 ``active_job_status``：前端据此决定「生成中」按钮态与轮询策略）。

    ``active_job_error`` 取「进行中任务」或**最近一条任务**的失败原因 —— 终态后
    ``active_thread_id`` 被清空，只有回退到最近一条才能看到「上一次为什么失败」。
    """
    service = _service(request, session, user)
    product = await service.get_or_404(to_uuid(product_id))
    job = await service.active_job(product)
    payload = serialize_product(product)
    payload["active_job_status"] = job.status if job is not None else None
    error_job = job if job is not None else await service.latest_job(product)
    payload["active_job_error"] = error_job.error if error_job is not None else None
    return ok(payload)


@router.patch("/{product_id}")
async def update_product(
    product_id: str,
    body: ProductUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """部分更新商品（``raw_images`` 为整体覆盖）。"""
    service = _service(request, session, user)
    product = await service.update(to_uuid(product_id), body.model_dump(exclude_unset=True))
    return ok(serialize_product(product))


@router.post("/{product_id}/generate")
async def trigger_generation(
    product_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """触发生成：登记任务 + 商品转 ``generating`` + 投递 ``job:generate``（含合规快照）。"""
    service = _service(request, session, user)
    job = await service.trigger_generation(to_uuid(product_id))
    return ok(
        {
            "thread_id": str(job.thread_id),
            "status": job.status,
            "message": "任务已投递；可经流式接口（P5）或商品状态跟踪进度",
        }
    )


@router.delete("/{product_id}")
async def delete_product_not_supported(
    product_id: str,
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """**已下线**：``DELETE /products/{id}``（原软删）。

    为什么保留这个显式 405 而不是直接让路由消失:
        直接删掉后，老前端/脚本会收到框架的「方法不允许/未匹配」，而响应体形状取决于
        框架兜底 —— 显式返回 405 + 可读原因，能把「删除只有彻底删除一条路」这件事
        直接告诉调用方（而不是让人以为是自己写错了 URL）。

    为什么权限仍收在 ``admin``:
        端点虽已下线，但「谁能调用删除类接口」属 RBAC 红线：若放开给所有登录角色，
        非管理员拿到的是 405（而非 403）—— 等于把「越权调用删除接口」从「被拒绝」
        变成「你调错了」，审计与告警都会丢掉信号。故保持原权限：非 admin → 403，admin → 405 + 指引。
    """
    raise ApiError(405, "已下线：删除只有「彻底删除」一条路径，请使用 DELETE /products/{id}/purge")


@router.delete("/{product_id}/purge")
async def purge_product(
    product_id: str,
    body: PurgeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """彻底删除：审计留痕 + 物理删行 + 清理自有对象 + 通知 ai-engine 清理 AI 域数据。

    返回里的 ``purge_enqueued`` 表示清理消息是否成功投递：失败**不影响**删除结果
    （行已删、审计已落库），但需要运维补投或人工清理，故必须回传给调用方而不是静默吞掉。
    """
    service = _service(request, session, user)
    result = await service.purge(to_uuid(product_id), actor_user_id=user.id, reason=body.reason)
    return ok(result)
