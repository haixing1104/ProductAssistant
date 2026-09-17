"""统一错误模型与响应封装。

为什么自己定义 ``ApiError``（而不是到处 ``raise HTTPException``）:
    · 业务层（services / repositories）不该 import FastAPI —— 那会让 service 层与 Web 框架耦合，
      测试与未来换框架都要重写；
    · 一处集中注册异常处理器，保证**所有**错误（含未捕获异常）都落成同一个响应体形状
      ``{"code", "data", "message"}``，前端只需要写一份错误处理。

响应形状（全站唯一）:
    成功：``{"code": 200, "data": <payload>, "message": "ok"}``
    失败：``{"code": <http status>, "data": null, "message": "<可读原因>"}``
    —— ``code`` 与 HTTP 状态码同值，前端既可读 body 也可直接用状态码分流。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = ["ApiError", "envelope", "install_exception_handlers", "ok"]


def ok(data: Any, message: str = "ok") -> dict[str, Any]:
    """成功响应体（路由统一用它包装返回值）。

    参数:
        data: 业务数据（任意 JSON 可序列化对象）。
        message: 提示文案（默认 ``ok``）。
    返回:
        ``{"code": 200, "data": data, "message": message}``。
    """
    return {"code": 200, "data": data, "message": message}


def envelope(payload: Any, code: int = 200, message: str = "ok") -> dict[str, Any]:
    """任意响应体（保留给需要非 200 语义但又要走封装体的场景）。"""
    return {"code": code, "data": payload, "message": message}


class ApiError(Exception):
    """业务异常：由 service 层抛出，由异常处理器统一转成响应体。

    属性:
        status_code: HTTP 状态码（也用作风包体里的 ``code``）。
        detail: 可读原因（面向用户，不要泄露内部实现细节）。
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """初始化。

        参数:
            status_code: HTTP 状态码。
            detail: 可读错误原因。
        返回:
            无返回值。
        """
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def install_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器（ApiError / HTTPException / 校验错误 / 兜底 500）。

    参数:
        app: FastAPI 实例。
    返回:
        无返回值。
    注意:
        · **必须注册 Starlette 的 ``HTTPException``**（而不是 FastAPI 的）：
          「路由未匹配」这类 404 由 Starlette 抛 ``starlette.exceptions.HTTPException``，
          而 ``fastapi.HTTPException`` 是它的**子类** —— 只注册子类会导致未匹配路由
          漏出框架默认的 ``{"detail": ...}``，破坏「全站统一信封」的承诺（实测踩过）；
        · 兜底 500 **不回显异常内容**（可能含 DSN、SQL 片段等敏感信息），只回通用文案；
          真实原因靠日志与 ``X-Request-Id`` 定位（见 ``middleware/observability.py``）。
    """

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        """业务异常 → 统一响应体。"""
        return JSONResponse(status_code=exc.status_code, content=envelope(None, exc.status_code, exc.detail))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架异常（含 404 未匹配路由、鉴权 401/403）→ 同一响应体形状。

        注意:
            **必须把 ``exc.headers`` 原样带过去**（如 429 的 ``Retry-After``、401 的
            ``WWW-Authenticate``）—— 自定义处理器若丢掉它们，客户端就失去了重试节流的唯一信号，
            限流会表现为「一直 429 但不知道该等多久」（实测踩过）。
        """
        detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(None, exc.status_code, detail),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """请求体/参数校验失败 → 400（把首个字段错误转成可读文案）。"""
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = f"参数校验失败：{loc} {first.get('msg', '')}".strip()
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=envelope(None, status.HTTP_400_BAD_REQUEST, message),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        """兜底：打印堆栈（含 request-id）但对外不泄露细节。"""
        print(f"[backend] 未捕获异常: {exc!r}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=envelope(None, status.HTTP_500_INTERNAL_SERVER_ERROR, "服务内部错误"),
        )
