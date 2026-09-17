"""OSS 预签名直传路由。

两步直传模型（P7 前端配合）:
    ① ``POST /oss/presign``（登录态）→ 返回 ``upload_url`` + ``public_url``；
    ② 浏览器 ``PUT upload_url``，**必须带与请求相同的 Content-Type**（它参与签名）；
    ③ 把 ``public_url`` 写入商品（``PATCH /products/{id}`` 的 ``raw_images`` 整体覆盖）。

为什么服务端不接收字节:
    见 ``services/oss.py`` 模块 docstring；简言之：内存/带宽/超时口径都不合适。

未配置 OSS 时:
    返回 503 并给出「未配置」原因 —— 明确失败比返回一个假 URL 好（后者会让前端在上传阶段
    才以 403 形式失败，排查成本高得多）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..repositories.products import ProductRepo
from ..schemas.api import PresignRequest
from ..services.oss import OssStorage, build_oss_storage
from .common import NOT_FOUND_DETAIL, ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/oss", tags=["oss"])

#: 图片上传允许的角色（reviewer 只审批，不需要上传）
UPLOAD_ROLES = ("admin", "operator")


@router.post("/presign")
async def presign_upload(
    body: PresignRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*UPLOAD_ROLES)),
) -> dict:
    """签发图片上传 URL（对象键固定落在 ``img/pa/{env}/{org}/{product}/raw/`` 下）。"""
    storage: OssStorage | None = build_oss_storage(request.app.state.settings)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OSS 未配置（需要 OSS_ENDPOINT/OSS_BUCKET/OSS_ACCESS_KEY_ID/OSS_ACCESS_KEY_SECRET）",
        )
    product_id = to_uuid(str(body.product_id))
    if await ProductRepo(session, user.org_id).get(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    key = storage.build_upload_key(
        env=request.app.state.settings.env,
        org_id=user.org_id,
        product_id=str(product_id),
        content_type=body.content_type,
    )
    upload_url, public_url, expires_in = storage.presign_put(key, content_type=body.content_type)
    return ok(
        {
            "upload_url": upload_url,
            "public_url": public_url,
            "object_key": key,
            "expires_in": expires_in,
            "content_type": body.content_type,
            "max_upload_bytes": request.app.state.settings.oss_max_upload_bytes,
        }
    )
