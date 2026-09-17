"""backend-api 应用工厂：中间件 / 路由 / 探针 / 常驻任务的装配处。

装配顺序（顺序即语义，勿随意调整）:
    ① ``app.state`` 注入 settings / engine / session_factory —— 依赖注入的唯一来源（测试可覆盖）；
    ② 异常处理器（保证**所有**响应都是统一信封）；
    ③ CORS（仅本地开发需要；生产同源反代时白名单为空即不启用）；
    ④ 请求上下文中间件（请求 ID + 访问日志）；
    ⑤ 路由（全部挂在 ``/api/v1`` 前缀下）；
    ⑥ 探针（``/healthz`` 存活、``/readyz`` 依赖可达）。

三个常驻任务（lifespan 内启动，仅在非测试环境）:
    · ``WorkflowResultConsumer``：消费 ``result:workflow`` 推进状态机（核心闭环）；
    · ``OutboxDeliverer``：投递审批通知（outbox → 钉钉/控制台）；
    · ``ApprovalRedriveWatchdog``：补投悬挂的审批 resume（下游抖动兜底）；
    · ``GenerationJobReaper``：回收僵死的生成任务（卡住的 job 会让商品永久 409、按钮不可点）。
    测试环境（``PA_ENV=test``）**不启动**：用例直接调用它们的单次处理方法，
    否则用例之间会互相抢消息（与 ProductPilot 同一取舍）。

探针语义（生产编排直接依赖，勿混用）:
    · ``/healthz``：进程活着即可 —— **不查依赖**。用它做存活探针，否则 PG 抖动会引发容器被反复重启；
    · ``/readyz``：DB ``SELECT 1`` + Redis ``PING`` —— 任一不可达即 503，由编排层摘流量（而不是重启）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.responses import JSONResponse

from .core.config import Settings, get_settings
from .core.db import create_engine_and_session
from .core.errors import envelope, install_exception_handlers
from .core.logging import configure_logging
from .core.redis_client import new_async_redis
from .middleware.observability import RequestContextMiddleware
from .routers import ALL_ROUTERS
from .services.approval_watchdog import ApprovalRedriveWatchdog
from .services.generation_job_reaper import GenerationJobReaper
from .services.notifications.deliverer import OutboxDeliverer
from .services.notifications.resolver import build_senders, resolve_channels
from .services.workflow_result_consumer import WorkflowResultConsumer

__all__ = ["create_app", "app"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂（测试用自定义 Settings 覆盖依赖）。

    参数:
        settings: 应用配置；None 时读环境变量（``get_settings()``）。
    返回:
        配置完成的 FastAPI 实例。
    """
    settings = settings or get_settings()
    # 最早处装配日志：否则 root logger 无 handler → ``pa_backend.access`` 的 INFO 访问日志
    # 会被 lastResort 静默丢弃，且所有日志没有时间戳（见 core/logging.py 的说明）
    configure_logging(settings)
    engine, session_factory = create_engine_and_session(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """进程生命周期：启动三个常驻任务，停机时优雅取消并释放连接池。"""
        tasks: list[asyncio.Task] = []
        stops: list[asyncio.Event] = []
        if settings.env != "test":
            channels = resolve_channels(settings)
            consumer = WorkflowResultConsumer(
                session_factory, settings=settings, notify_channels=channels
            )
            deliverer = OutboxDeliverer(
                session_factory, settings=settings, senders=build_senders(settings, channels)
            )
            watchdog = ApprovalRedriveWatchdog(session_factory, settings=settings)
            reaper = GenerationJobReaper(session_factory, settings=settings)
            print(
                f"[backend] env={settings.env} 启动常驻任务：result 消费器 / 通知投递器"
                f"（渠道={list(channels)}）/ 审批补投守护 / 僵死任务回收"
            )
            for runner in (consumer.run, deliverer.run, watchdog.run, reaper.run):
                stop = asyncio.Event()
                stops.append(stop)
                tasks.append(asyncio.create_task(runner(stop)))
        else:
            print("[backend] env=test：不启动常驻任务（用例直接调用它们的单次处理方法）")
        try:
            yield
        finally:
            for stop in stops:
                stop.set()
            for task in tasks:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 停机不能因任务异常而中断
                    pass
            # 停机：释放连接池（Redis 客户端由各自调用点关闭）
            await engine.dispose()

    app = FastAPI(title="ProductAssistant backend-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    install_exception_handlers(app)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,  # refresh cookie 需要（跨域 dev 下必须显式允许凭据）
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Total-Count", "X-Request-Id"],
        )
    app.add_middleware(RequestContextMiddleware)

    for router in ALL_ROUTERS:
        app.include_router(router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针：进程在即 200（刻意不查依赖，见模块 docstring）。"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        """就绪探针：DB + Redis 均可达才 200，否则 503（编排层据此摘流量）。"""
        checks: dict[str, str] = {}
        try:
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 探针自身不得抛异常
            checks["database"] = f"error: {type(exc).__name__}"
        redis_client = new_async_redis(request.app.state.settings)
        try:
            await redis_client.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {type(exc).__name__}"
        finally:
            await redis_client.aclose()
        if any(value != "ok" for value in checks.values()):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=envelope(checks, status.HTTP_503_SERVICE_UNAVAILABLE, "依赖不可用"),
            )
        return envelope(checks, 200, "ready")

    return app


app = create_app()
