"""应用骨架与探针用例（需要真实 PostgreSQL + Redis；未配置 DSN 自动 skip）。

覆盖：
    · ``/healthz`` 存活探针不查依赖（依赖全挂也要 200 —— 否则编排层会误重启容器）；
    · ``/readyz`` 就绪探针把 DB/Redis 的可达性如实报出；
    · 统一响应信封（含 404 与请求 ID 透传）。
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from pa_backend.core.config import Settings


async def test_healthz_does_not_touch_dependencies(client: AsyncClient):
    """存活探针：进程在即 200（与依赖状态无关）。"""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_reports_dependencies(client: AsyncClient, settings: Settings):
    """就绪探针：DB 与 Redis 均可达时 200，并逐项回报状态。"""
    resp = await client.get("/readyz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] == {"database": "ok", "redis": "ok"}


async def test_readyz_degrades_to_503_when_redis_unreachable(app: FastAPI, settings: Settings):
    """Redis 不可达 → 503（编排层摘流量，而不是重启进程）。

    通过把 ``app.state.settings`` 换成指向黑洞端口的配置来模拟，不改全局环境变量。
    """
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:6399/0"})
    app.state.settings = broken
    from httpx import ASGITransport, AsyncClient as _Client

    async with _Client(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["data"]["redis"].startswith("error:")
    assert resp.json()["data"]["database"] == "ok"


async def test_unknown_route_uses_unified_envelope(client: AsyncClient):
    """未知路由也必须落成统一信封（前端只写一份错误处理）。"""
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"code", "data", "message"}
    assert body["code"] == 404 and body["data"] is None


async def test_request_id_is_generated_and_echoed(client: AsyncClient):
    """请求 ID：未带则生成并回写响应头；带了则原样透传（便于跨进程串日志）。"""
    generated = await client.get("/healthz")
    assert generated.headers.get("X-Request-Id")

    echoed = await client.get("/healthz", headers={"X-Request-Id": "trace-abc-123"})
    assert echoed.headers.get("X-Request-Id") == "trace-abc-123"


async def test_cors_preflight_allows_configured_origin(app: FastAPI):
    """配置了白名单时，预检请求应放行该来源（本地 dev 前端 5173 → 8000）。"""
    from httpx import ASGITransport, AsyncClient as _Client

    settings = app.state.settings
    async with _Client(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.options(
            "/healthz",
            headers={
                "Origin": settings.cors_origin_list[0],
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == settings.cors_origin_list[0]
