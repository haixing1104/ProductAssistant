"""可观测性中间件：请求 ID 透传 + 一行式访问日志。

为什么要有请求 ID:
    这个系统里有三条异步链路（前端 → backend → Redis Streams → ai-engine），一次「生成为什么失败」
    的排查要跨两个进程三套日志。请求 ID 是**把一次 HTTP 请求在 backend 侧的所有日志串起来**的最小手段；
    前端/网关带上 ``X-Request-Id`` 时原样透传，便于端到端串接。

为什么用 ContextVar 而不是往每个函数传参:
    service / repository 层不应被迫接收 ``request``（会与 Web 框架耦合）；ContextVar 让
    「当前请求 ID」在任何深度都能读到，且**在请求结束时 reset**（避免串到下一个请求 —— 这正是
    ContextVar 与全局变量的区别）。

为什么不用 ``print`` 而是自带 ``logger``:
    访问日志是运维输入（可被采集），业务日志是排障输入；两者混在一起会让「谁在报错」变得模糊。
    这里统一经 ``logging``（默认 root logger，部署层可接管 handler/JSON 化）。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["RequestContextMiddleware", "current_request_id"]

_logger = logging.getLogger("pa_backend.access")

#: 当前请求 ID（请求内任意深度可读；请求结束即 reset）
_request_id_var: ContextVar[str] = ContextVar("pa_request_id", default="")

#: 响应头名（网关/前端据此回传同一个 ID）
HEADER_REQUEST_ID = "X-Request-Id"


def current_request_id() -> str:
    """读取当前请求 ID（无请求上下文时返回空串，不抛异常）。"""
    return _request_id_var.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """给每个请求分配/透传 ID，并记录访问日志。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """处理单个请求。

        参数:
            request: 入站请求。
            call_next: 下游处理链。
        返回:
            下游响应（附加 ``X-Request-Id`` 头）。
        注意:
            · 未捕获异常也必须**记录耗时并重新抛出**（否则 500 的日志会缺一条，
              而「失败请求没日志」是最误导人的现象）；
            · 日志里的 ``org`` / ``user`` 取自 ``request.state``（由 ``deps.get_current_user`` 写入），
              未认证请求自然为空 —— 不额外查库。
        """
        request_id = (request.headers.get(HEADER_REQUEST_ID) or "").strip() or uuid.uuid4().hex[:16]
        token = _request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._log(request, status_code=500, elapsed_ms=elapsed_ms)
            raise
        finally:
            _request_id_var.reset(token)
        response.headers[HEADER_REQUEST_ID] = request_id
        self._log(
            request,
            status_code=response.status_code,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        return response

    @staticmethod
    def _log(request: Request, *, status_code: int, elapsed_ms: float) -> None:
        """输出一行访问日志（字段顺序固定，便于 grep/采集）。"""
        org = getattr(request.state, "org_id", "") or "-"
        user = getattr(request.state, "user_id", "") or "-"
        _logger.info(
            "%s %s %s %.1fms request_id=%s org=%s user=%s",
            request.method,
            request.url.path,
            status_code,
            elapsed_ms,
            getattr(request.state, "request_id", "-"),
            org,
            user,
        )
