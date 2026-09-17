"""成员管理路由（admin 专用）：列表 / 新增 / 停用。

RBAC 与自我保护（两条都必要，缺一条都会留下事故面）:
    · 只有 ``admin`` 能进（``require_roles("admin")``）；
    · 通过本接口只能创建 ``reviewer`` / ``operator`` —— ``admin`` 只能由注册流程产生（防提权）；
    · 不得停用 ``admin`` 账号：接口调用者必然是 admin，而管理员只能由注册产生，
      因此这一条同时挡住了「把自己关在门外」与「停掉本租户最后一个管理员」（见接口 docstring）。

分页:
    ``offset`` / ``limit`` 查询参数；总数通过 ``X-Total-Count`` 响应头回传（前端读头部即可，
    响应体保持数组，便于复用一份列表渲染逻辑）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..models.orm import SysUser
from ..repositories.users import UserRepo, is_username_conflict
from ..schemas.api import MemberCreateRequest, MemberResponse
from ..core.security import hash_password
from .common import NOT_FOUND_DETAIL, ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: SysUser) -> dict:
    """ORM → 响应体（``last_login_at`` 转 ISO 字符串；**绝不**带 hashed_password）。"""
    return MemberResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    ).model_dump(mode="json")


@router.get("")
async def list_members(
    response: Response,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """列出本组织成员（分页；总数走 ``X-Total-Count``）。"""
    items, total = await UserRepo(session, user.org_id).list_members(offset=offset, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return ok([_to_response(item) for item in items])


@router.post("")
async def create_member(
    body: MemberCreateRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """新增成员（``reviewer`` / ``operator``；同名按组织内唯一约束返回 409）。"""
    repo = UserRepo(session, user.org_id)
    try:
        created = await repo.create(
            username=body.username, hashed_password=hash_password(body.password), role=body.role
        )
    except IntegrityError as exc:
        await session.rollback()
        if is_username_conflict(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该用户名已存在") from exc
        raise
    await session.commit()
    return ok(_to_response(created))


@router.post("/{member_id}/disable")
async def disable_member(
    member_id: str,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles("admin")),
) -> dict:
    """停用成员（软禁用：``status='disabled'``，历史记录保留）。

    为什么「禁止停用任意 admin」这一条规则就足够（不必再单写「不能停用自己」）:
        本接口的调用者**必然是 admin**（``require_roles("admin")``），而管理员只能由注册流程产生
        （本模块没有 promote 接口）。所以「admin 一律不可停用」天然覆盖了「不能把自己关在门外」
        与「不能把本租户最后一个管理员停掉」两种事故 —— 单写那两条判断反而是**不可达的死代码**。
        需要增减管理员时：新增走注册/直连库，删除直连库（运维动作，留痕在 DB 侧）。
    """
    target_id = to_uuid(member_id, detail=NOT_FOUND_DETAIL)
    repo = UserRepo(session, user.org_id)
    target = await repo.get(target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if target.status == "disabled":
        return ok(_to_response(target))  # 幂等：重复停用直接返回当前状态
    if target.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="管理员账号不可停用（管理员增删只能经注册流程或直接改库）",
        )
    target.status = "disabled"
    await session.commit()
    return ok(_to_response(target))
