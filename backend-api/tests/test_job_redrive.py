"""补投用例：redrive 接口 + 补投守护（判据 + 节流）。

故障场景（本文件的被测对象）:
    ``decide`` 先 commit 审批结论再 XADD resume；若 XADD 失败，审批结论已生效但商品仍停在
    ``waiting_approval`` —— 这时需要「补投」把流程推完。两条路：
    ① 人工：``POST /approvals/{id}/redrive``（admin）；
    ② 自动：``ApprovalRedriveWatchdog`` 周期扫描悬挂单（带 Redis 节流，避免反复投递）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.approval_watchdog import ApprovalRedriveWatchdog
from pa_backend.services.event_envelope import decode_fields
from tests.conftest import SeededOrg, _exec, seed_job

APPROVALS_URL = "/api/v1/approvals"


def _seed_decided_approval(
    backend_dsn: str,
    *,
    org_id: str,
    product_id: str,
    status: str = "approved",
    resolved_seconds_ago: int = 600,
) -> tuple[str, str]:
    """造一条**已定案**的单子（商品仍在待审批 = 悬挂），返回 (thread_id, approval_id)。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=org_id,
        product_id=product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    approval_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals "
        "(id, org_id, product_id, thread_id, status, channel, feedback, resolved_at) "
        "VALUES (%s, %s, %s, %s, %s, 'web', '历史反馈', %s)",
        (
            approval_id,
            org_id,
            product_id,
            thread_id,
            status,
            datetime.now(timezone.utc) - timedelta(seconds=resolved_seconds_ago),
        ),
    )
    return thread_id, approval_id


def _read_approval_messages(settings: Settings) -> list[dict]:
    """读 ``job:approval`` 流。"""
    client = new_sync_redis(settings)
    try:
        entries = client.xrange(RedisKeys(settings.env).job_approval(), min="-", max="+", count=200)
        return [decode_fields(fields) for _id, fields in entries]
    finally:
        client.close()


def _drain(settings: Settings) -> None:
    """清空审批流（断言只看本次投递）。"""
    client = new_sync_redis(settings)
    try:
        client.delete(RedisKeys(settings.env).job_approval())
    finally:
        client.close()


async def test_redrive_endpoint_reenqueues_decided_approval(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """人工补投：已定案单可重投（载荷带原反馈），幂等可重复执行。"""
    thread_id, approval_id = _seed_decided_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    _drain(settings)
    resp = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["resume_enqueued"] is True
    messages = _read_approval_messages(settings)
    assert len(messages) == 1
    assert messages[0]["thread_id"] == thread_id
    assert messages[0]["result"] == "approved"
    assert messages[0]["feedback"] == "历史反馈"


async def test_redrive_rejects_pending_approval(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """仍在待审的单子不能补投 → 409（应先审批，否则补投无意义）。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    approval_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals (id, org_id, product_id, thread_id, status, channel) "
        "VALUES (%s, %s, %s, %s, 'pending', 'web')",
        (approval_id, seeded_org.org_id, seeded_org.product_id, thread_id),
    )
    resp = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)
    assert resp.status_code == 409


async def test_redrive_requires_admin(client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg):
    """补投是运维动作 → 只有 admin（reviewer 403）。"""
    from tests.test_members_rbac import _create_member, _login

    _thread, approval_id = _seed_decided_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    resp = await client.post(
        f"{APPROVALS_URL}/{approval_id}/redrive", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- 补投守护


async def test_watchdog_redrives_stale_decided_approval_once(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """守护扫描：悬挂单被补投一次；随后被节流（第二次扫描不再重复投递）。"""
    thread_id, approval_id = _seed_decided_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    _drain(settings)
    # 清掉可能残留的节流键（Redis DB 在多次 compose run 之间会保留）
    client = new_sync_redis(settings)
    try:
        client.delete(RedisKeys(settings.env).approval_redrive(approval_id))
    finally:
        client.close()

    watchdog = ApprovalRedriveWatchdog(
        app.state.session_factory, settings=settings, min_age_seconds=60, throttle_seconds=3600
    )
    assert await watchdog.scan_once() == 1
    messages = _read_approval_messages(settings)
    assert [item["thread_id"] for item in messages] == [thread_id]

    # 第二次扫描：节流键仍在 → 不再重复投递
    assert await watchdog.scan_once() == 0
    assert len(_read_approval_messages(settings)) == 1


async def test_watchdog_ignores_approval_whose_product_moved_on(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """守护不碰「商品已被 resume 推进」的单子（避免对正常流程重复 resume）。"""
    _thread, approval_id = _seed_decided_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.products SET status = 'published' WHERE id = %s",
        (seeded_org.product_id,),
    )
    watchdog = ApprovalRedriveWatchdog(app.state.session_factory, settings=settings, min_age_seconds=60)
    assert await watchdog.scan_once() == 0
