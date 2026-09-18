"""僵死生成任务回收（reaper）与「守卫宽容」用例。

故障场景（本文件的被测对象）:
    job 卡在 ``running``（worker 被杀 / 消息丢失 / 图挂起）→ ``trigger_generation`` 永久 409，
    前端按钮也因 ``status='generating'`` 被禁用 → 商品**只能改库才能恢复**（2026-09 实测）。
    两道防线：
    ① ``GenerationJobReaper``：后台周期把超期任务判死（job→failed、商品→draft）；
    ② ``trigger_generation`` 的**守卫宽容**：命中超期任务时先就地回收再放行本次触发（用户自助）。

判据只用 DB 时间戳（``updated_at``）。**刻意不读** ``pa:{env}:lock:{thread_id}``：
那是 worker↔worker 的内部互斥键，契约规定 backend 不得读写（core/keys.py 未暴露，有测试钉住）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.services.generation_job_reaper import (
    DEFAULT_STALE_MINUTES,
    GenerationJobReaper,
    is_stale,
)
from tests.conftest import SeededOrg, _exec, seed_job

GENERATE_URL = "/api/v1/products/{product_id}/generate"
PRODUCTS_URL = "/api/v1/products"


def _job_row(backend_dsn: str, thread_id: str) -> dict:
    """读任务行的（status, error, updated_at）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(backend_dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, error, updated_at FROM schema_pa_backend.generation_jobs WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    assert row is not None, "任务行应当存在"
    return dict(row)


def _product_row(backend_dsn: str, product_id: str) -> dict:
    """读商品的（status, active_thread_id）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(backend_dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, active_thread_id FROM schema_pa_backend.products WHERE id = %s",
            (product_id,),
        ).fetchone()
    assert row is not None, "商品行应当存在"
    return dict(row)


def _reaper(app, settings: Settings, *, stale_minutes: int = DEFAULT_STALE_MINUTES) -> GenerationJobReaper:
    """构造被测 reaper（单轮扫描，不启动常驻循环）。"""
    return GenerationJobReaper(app.state.session_factory, settings=settings, stale_minutes=stale_minutes)


# ---------------------------------------------------------------- 纯函数判据


def test_is_stale_judgement(seeded_org: SeededOrg):
    """判据：只在超期且状态仍是候选态时判死（终态永不回收；naive 时间戳不漏判）。"""
    import uuid

    from pa_backend.models.orm import GenerationJob

    now = datetime.now(timezone.utc)

    def _job(status: str, updated_at: datetime) -> GenerationJob:
        """造一个内存态任务行（只填 ``is_stale`` 判据用到的字段）。

        为什么不用真库: 本用例只验证「超期判据」这一个纯函数，构造真实行会让
        时间戳受 DB 时钟/时区影响；这里显式传入 ``updated_at`` 才能钉住边界。
        """
        return GenerationJob(
            id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            org_id=uuid.UUID(seeded_org.org_id),
            product_id=uuid.UUID(seeded_org.product_id),
            status=status,
            updated_at=updated_at,
        )

    assert is_stale(_job("running", now - timedelta(minutes=20)), stale_minutes=15, now=now) is True
    assert is_stale(_job("waiting_input", now - timedelta(minutes=20)), stale_minutes=15, now=now) is True
    assert is_stale(_job("running", now - timedelta(seconds=30)), stale_minutes=15, now=now) is False
    assert is_stale(_job("succeeded", now - timedelta(days=3)), stale_minutes=15, now=now) is False
    # naive 时间戳（历史脏数据/手工构造）：不能因「看起来是未来」而漏判
    naive = (now - timedelta(minutes=20)).replace(tzinfo=None)
    assert is_stale(_job("running", naive), stale_minutes=15, now=now) is True


# ---------------------------------------------------------------- reaper 单轮扫描


async def test_reaper_releases_stale_job_and_product(
    app, settings: Settings, backend_dsn: str, seeded_org: SeededOrg
):
    """超期任务 → job=failed（带原因）、商品回 draft 且清空 active_thread_id。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        age_minutes=DEFAULT_STALE_MINUTES + 5,
    )
    reaped = await _reaper(app, settings).scan_once()
    assert [item["thread_id"] for item in reaped] == [thread_id]
    assert reaped[0]["product_released"] is True

    job = _job_row(backend_dsn, thread_id)
    assert job["status"] == "failed"
    assert "自动回收" in (job["error"] or "")
    product = _product_row(backend_dsn, seeded_org.product_id)
    assert product["status"] == "draft"
    assert product["active_thread_id"] is None


async def test_reaper_leaves_fresh_job_alone(
    app, settings: Settings, backend_dsn: str, seeded_org: SeededOrg
):
    """未超期（仍在正常处理窗口）→ 一律不动：**宁可晚收也不误杀**。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    assert await _reaper(app, settings).scan_once() == []
    assert _job_row(backend_dsn, thread_id)["status"] == "running"
    product = _product_row(backend_dsn, seeded_org.product_id)
    assert product["status"] == "generating"
    assert str(product["active_thread_id"]) == thread_id


async def test_reaper_skips_jobs_already_terminal(
    app, settings: Settings, backend_dsn: str, seeded_org: SeededOrg
):
    """已终态（succeeded/failed）永不回收 —— 幂等，避免重复写库。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="succeeded",
        product_status="published",
        age_minutes=600,
    )
    assert await _reaper(app, settings).scan_once() == []
    assert _job_row(backend_dsn, thread_id)["status"] == "succeeded"


async def test_reaper_does_not_disturb_product_taken_over_by_newer_thread(
    app, settings: Settings, backend_dsn: str, seeded_org: SeededOrg
):
    """旧线程僵死但商品已指向新线程 → 只收口旧 job，**不动商品**（不打扰新任务）。"""
    old_thread = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        age_minutes=DEFAULT_STALE_MINUTES + 5,
        bind_active_thread=False,  # 商品 active_thread_id 不指向它
    )
    new_thread = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    reaped = await _reaper(app, settings).scan_once()
    assert [item["thread_id"] for item in reaped] == [old_thread]
    assert reaped[0]["product_released"] is False
    assert _job_row(backend_dsn, old_thread)["status"] == "failed"
    product = _product_row(backend_dsn, seeded_org.product_id)
    assert product["status"] == "generating"
    assert str(product["active_thread_id"]) == new_thread  # 仍归新线程


# ---------------------------------------------------------------- 守卫宽容（触发路径）


async def test_trigger_reaps_stale_job_and_proceeds(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """守卫宽容：商品卡在僵死任务上时，用户**点一次生成就能自愈**（不再永久 409）。"""
    stale_thread = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        age_minutes=DEFAULT_STALE_MINUTES + 5,
    )
    resp = await client.post(
        GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    new_thread = resp.json()["data"]["thread_id"]
    assert new_thread != stale_thread

    # 旧任务被回收为 failed（不再悬挂），商品改由新线程接管
    assert _job_row(backend_dsn, stale_thread)["status"] == "failed"
    product = _product_row(backend_dsn, seeded_org.product_id)
    assert product["status"] == "generating"
    assert str(product["active_thread_id"]) == new_thread

    # 详情接口把新任务的 active_job_status 带出来（前端据此显示「生成中」并连流）
    detail = await client.get(f"{PRODUCTS_URL}/{seeded_org.product_id}", headers=auth_headers)
    assert detail.json()["data"]["active_job_status"] == "running"


async def test_trigger_still_409_while_job_is_fresh(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """未超期的进行中任务**照旧拦住**（防重复扣费；守卫宽容只对僵死任务生效）。"""
    live_thread = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    resp = await client.post(
        GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers
    )
    assert resp.status_code == 409, resp.text
    assert live_thread in resp.text
    assert _job_row(backend_dsn, live_thread)["status"] == "running"
