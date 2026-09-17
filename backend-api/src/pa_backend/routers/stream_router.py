"""SSE 流式端点：短时票据换连接 + 历史回放 + 实时尾随（打字机/阶段/图片就绪的唯一数据源）。

鉴权模型（两层）:
    · ``GET /products/{id}/stream-ticket``：用 Bearer access 换**短时签名票据**
      （``kind=sse`` + **绑定 product_id**，TTL = ``BACKEND_STREAM_TICKET_TTL_SECONDS``）。
      为什么要票据：access 只有 15 分钟，而一次生成的流可能更久；
      票据把「长连接」与「短时效凭证」解耦，且泄露面仅限单个商品；
    · ``GET /products/{id}/stream?ticket=…``：优先票据；无票据时兼容 Bearer（便于 curl 排查）。

连接生命周期:
    ① 校验身份与商品归属（越权/不存在 → 404，不泄露存在性）；
    ② **状态门**：``products.status ∈ {generating, waiting_approval}`` 才回放/尾随；
       其余状态（含已结束但残留 ``active_thread_id`` 的脏数据）直接发一次 ``ready`` 控制帧并关流
       —— 否则前端会把已结束的任务误判为「生成中」而永久空转重连；
    ③ 回放 ``evt:{thread_id}`` 历史（支持 ``Last-Event-ID`` 续传，``XRange`` 独占下界）；
    ④ 每 ~0.25s 增量尾随，直到终态事件（``done``/``rejected``/``failed``/``hitl.waiting``）或空闲超时；
    ⑤ 空闲关流发注释帧 ``: stream-idle-close``（不产生事件），前端据此带 ``Last-Event-ID`` 自动续连。

两类**backend 生成的控制帧**（不在 ai-engine 的 15 类事件清单里，前端需区分）:
    · ``ready``：无进行中任务，可直接渲染终态、无需重连；
    · 注释帧（以 ``:`` 开头）：SSE 协议级提示，不会触发 ``onmessage``。

**SSE 的错误必须发生在开始写响应之前**:
    401/404 都在生成器启动前抛出（此时还能返回统一 JSON 信封）；
    一旦开始流式响应，HTTP 状态码与响应体都已定型，读流异常只能用注释帧告知前端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from ..core import security
from ..core.deps import CurrentUser, bearer, get_current_user, get_db, require_roles
from ..repositories.products import ProductRepo
from ..services.stream_reader import TERMINAL_EVENT_TYPES, EvtStreamReader
from .common import NOT_FOUND_DETAIL, ok, to_uuid

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["stream"])

#: 读接口允许的角色（三种角色都能看流：生成过程不是敏感信息）
READ_ROLES = ("admin", "operator", "reviewer")

#: 「有进行中任务」的权威信号（与前端按钮态、消费器状态机共用同一枚举）
ACTIVE_STREAM_STATUSES = frozenset({"generating", "waiting_approval"})

#: 实时尾随轮询间隔（秒）：~250ms 兼顾打字机体感与 Redis 开销
POLL_INTERVAL_SECONDS = 0.25

#: 单次读取上限（避免一次拉太多把响应撑爆）
READ_BATCH = 200

#: SSE 帧分隔符（两条换行）
_NL2 = "\n\n"


def _format_event(message_id: str, payload: dict) -> str:
    """把一条 evt 载荷格式化成 SSE 帧（``id`` + ``data``）。"""
    body = {"type": payload.get("type"), "data": payload.get("data")}
    return f"id: {message_id}\ndata: {json.dumps(body, ensure_ascii=False)}{_NL2}"


def _last_event_id(request: Request) -> str | None:
    """取续传游标：优先 ``Last-Event-ID`` 头（浏览器自动带），其次 ``lastEventId`` 查询参数。"""
    raw = (request.headers.get("last-event-id") or request.query_params.get("lastEventId") or "").strip()
    return raw or None


async def _resolve_viewer(
    request: Request,
    session: AsyncSession,
    product_id: uuid.UUID,
    cred: HTTPAuthorizationCredentials | None,
    ticket: str | None,
) -> CurrentUser:
    """解析调用方身份（票据优先，Bearer 兜底），并确认商品归属。

    参数:
        request: 当前请求。
        session: DB 会话。
        product_id: 路径里的商品 ID。
        cred: Bearer 凭证（可空）。
        ticket: 查询参数里的 SSE 票据（可空）。
    返回:
        ``CurrentUser``（票据路径下由 claims 还原，username 置空）。
    异常:
        HTTPException: 401（无票据也无 Bearer / 票据无效或类型不符）、404（商品不存在或不属于该租户）。
    """
    settings = request.app.state.settings
    if ticket:
        try:
            claims = security.decode_token(ticket, settings)
        except Exception as exc:  # noqa: BLE001 过期/签名不符统一 401
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SSE 票据无效或已过期") from exc
        if claims.get("kind") != security.KIND_SSE:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="票据类型不符")
        if str(claims.get("product_id")) != str(product_id):
            # 票据只对签发的那个商品有效：拿 A 商品的票据看 B 商品的流 → 404（与越权同响应）
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
        viewer = CurrentUser(
            id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org_id") or ""),
            username="",
            role=str(claims.get("role") or "operator"),
        )
    elif cred is not None:
        viewer = await get_current_user(request, session, cred)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 SSE 票据或 Bearer Token")

    if await ProductRepo(session, viewer.org_id).get(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return viewer


@router.get("/{product_id}/stream-ticket")
async def issue_stream_ticket(
    product_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """用登录态换商品流的短时签名票据（前端在建立 SSE 前调用一次）。

    返回 ``ticket`` / ``expires_in`` / ``product_id``；票据**绑定该商品**且不含审批权限。
    """
    pid = to_uuid(product_id)
    if await ProductRepo(session, user.org_id).get(pid) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    settings = request.app.state.settings
    ticket = security.create_sse_ticket(
        user_id=user.id, org_id=user.org_id, role=user.role, product_id=str(pid), settings=settings
    )
    return ok({"ticket": ticket, "expires_in": settings.stream_ticket_ttl_seconds, "product_id": str(pid)})


@router.get("/{product_id}/stream")
async def stream(
    product_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    ticket: str | None = Query(default=None, description="stream-ticket 换来的短时票据"),
):
    """SSE：回放 + 尾随某商品的生成过程事件（详见模块 docstring）。"""
    settings = request.app.state.settings
    pid = to_uuid(product_id)
    viewer = await _resolve_viewer(request, session, pid, cred, ticket)
    product = await ProductRepo(session, viewer.org_id).get(pid)
    if product is None:  # _resolve_viewer 已校验；此处仅为类型收窄与防御（不改状态码语义）
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    # 生成器可能在会话作用域之外执行，故把要用的字段**提前取成局部变量**（不依赖 ORM 实例存活）
    product_status = product.status
    thread_id = str(product.active_thread_id) if product.active_thread_id else None
    active = product_status in ACTIVE_STREAM_STATUSES and thread_id is not None
    cursor = _last_event_id(request)
    idle_limit = float(settings.stream_idle_seconds)

    async def event_source():
        """SSE 生成器：``ready`` 短路 → 回放 → 尾随 → 终态/空闲关流。"""
        if not active:
            # ② 无进行中任务：一条 ready 控制帧后立即关流（前端不必重连）
            payload = {"type": "ready", "data": {"product_id": str(pid), "status": product_status}}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}{_NL2}"
            return

        reader = EvtStreamReader(settings)
        seen = cursor
        try:
            # ③ 历史回放（含续传：seen 非空时 XRange 用独占下界，不重发已收到的最后一条）
            history = await asyncio.to_thread(reader.history, thread_id, last_id=seen, count=READ_BATCH)
            for item in history:
                seen = item["seq"]
                yield _format_event(item["seq"], item["payload"])
                if item["payload"].get("type") in TERMINAL_EVENT_TYPES:
                    return
            # ④ 实时尾随：每 ~0.25s 增量读一次；连续无新事件累计到上限即空闲关流
            idle_seconds = 0.0
            while idle_seconds < idle_limit:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                items = await asyncio.to_thread(reader.poll, thread_id, last_id=seen, count=READ_BATCH)
                if not items:
                    idle_seconds += POLL_INTERVAL_SECONDS
                    continue
                idle_seconds = 0.0
                for item in items:
                    seen = item["seq"]
                    yield _format_event(item["seq"], item["payload"])
                    if item["payload"].get("type") in TERMINAL_EVENT_TYPES:
                        return
            # ⑤ 空闲关流：注释帧不产生事件，前端据此按 Last-Event-ID 续连
            yield f": stream-idle-close (last_id={seen or '-'}){_NL2}"
        except asyncio.CancelledError:
            raise  # 客户端主动断开：正常路径，不打错误日志
        except Exception as exc:  # noqa: BLE001 已开始流式响应，只能用注释帧告知
            logger.warning("SSE 读流异常 thread=%s: %r", thread_id, exc)
            yield f": stream-error{_NL2}"
        finally:
            await asyncio.to_thread(reader.close)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（nginx）必须关闭缓冲，否则打字机会被攒成一坨
            "X-Accel-Buffering": "no",
        },
    )
