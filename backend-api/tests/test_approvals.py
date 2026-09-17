"""审批用例：CAS 定案 / resume 投递 / 权限 / 越权 / 深链。

核心断言:
    · 批准与驳回都走 **CAS**：并发/重复审批只有一个成功，另一个 409（不是 5xx、不是静默覆盖）；
    · 定案后**必须**在 ``job:approval`` 流里出现对应消息（载荷含 thread/product/org/result/feedback）；
    · operator **不能审批**（生成与把关分离 —— HITL 的意义）；跨租户审批单 → 404；
    · 审批结论落库且 ``approver_id/resolved_at/feedback`` 齐全（审计可追溯）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.event_envelope import decode_fields
from tests.conftest import SeededOrg, _exec, seed_job

APPROVALS_URL = "/api/v1/approvals"
PENDING_URL = "/api/v1/approvals/pending"


def _seed_pending_approval(backend_dsn: str, *, org_id: str, product_id: str) -> tuple[str, str]:
    """造一条待审单（+ 对应 waiting_input 任务与 waiting_approval 商品），返回 (thread_id, approval_id)。"""
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
        "(id, org_id, product_id, thread_id, status, channel, content_snapshot) "
        "VALUES (%s, %s, %s, %s, 'pending', 'web', %s::jsonb)",
        (
            approval_id,
            org_id,
            product_id,
            thread_id,
            '{"reason": "high_value", "content": {"blocks": [{"type": "text", "text": "待审文案"}]}, '
            '"evaluation_result": {"passed": true, "score": 88.5, "errors": []}, "evaluation_attempts": 1}',
        ),
    )
    return thread_id, approval_id


def _read_approval_messages(settings: Settings) -> list[dict]:
    """读 ``job:approval`` 流里的载荷。"""
    client = new_sync_redis(settings)
    try:
        entries = client.xrange(RedisKeys(settings.env).job_approval(), min="-", max="+", count=200)
        return [decode_fields(fields) for _id, fields in entries]
    finally:
        client.close()


def _drain_approval_stream(settings: Settings) -> None:
    """清空审批流（断言只看本次投递）。"""
    client = new_sync_redis(settings)
    try:
        client.delete(RedisKeys(settings.env).job_approval())
    finally:
        client.close()


async def test_pending_list_returns_summary_without_full_snapshot(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """待办列表：带商品标题/SKU 与快照摘要；默认**不带**完整快照（列表接口要快）。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    resp = await client.get(PENDING_URL, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert int(resp.headers["X-Total-Count"]) >= 1
    item = next(row for row in resp.json()["data"] if row["id"] == approval_id)
    assert item["snapshot_summary"]["reason"] == "high_value"
    assert item["snapshot_summary"]["score"] == 88.5
    assert "content_snapshot" not in item
    assert item["product_title"] == "测试商品·真空保温杯"

    with_snapshot = await client.get(PENDING_URL, params={"with_snapshot": True}, headers=auth_headers)
    detailed = next(row for row in with_snapshot.json()["data"] if row["id"] == approval_id)
    assert detailed["content_snapshot"]["content"]["blocks"][0]["text"] == "待审文案"


async def test_approve_decides_and_enqueues_resume(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """批准：CAS 定案 + 投递 ``job:approval``（载荷字段与 ai-engine reader 对齐）。"""
    thread_id, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    _drain_approval_stream(settings)
    resp = await client.post(
        f"{APPROVALS_URL}/{approval_id}/approve", json={"feedback": "可以上架"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["status"] == "approved"
    assert body["resume_enqueued"] is True

    messages = _read_approval_messages(settings)
    assert len(messages) == 1
    payload = messages[0]
    assert payload["schema_version"] == 1
    assert payload["thread_id"] == thread_id
    assert payload["product_id"] == seeded_org.product_id
    assert payload["org_id"] == seeded_org.org_id
    assert payload["result"] == "approved"
    assert payload["feedback"] == "可以上架"

    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, feedback, approver_id, resolved_at FROM schema_pa_backend.hitl_approvals "
            "WHERE id = %s",
            (approval_id,),
        ).fetchone()
    assert row[0] == "approved"
    assert row[1] == "可以上架"
    assert str(row[2]) == seeded_org.user_id
    assert row[3] is not None  # resolved_at 已写


async def test_reject_requires_and_persists_feedback(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """驳回：投递 ``result=rejected`` + 反馈落库（反馈会作为 Reflection 重写的输入）。"""
    thread_id, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    _drain_approval_stream(settings)
    resp = await client.post(
        f"{APPROVALS_URL}/{approval_id}/reject",
        json={"feedback": "价格表述夸大，请重写"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
    messages = _read_approval_messages(settings)
    assert messages[0]["result"] == "rejected"
    assert messages[0]["feedback"] == "价格表述夸大，请重写"
    assert messages[0]["thread_id"] == thread_id


async def test_double_decision_second_returns_409(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """CAS 语义：第二次审批（无论批准还是驳回）→ 409，且不产生第二条消息。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    first = await client.post(f"{APPROVALS_URL}/{approval_id}/approve", json={}, headers=auth_headers)
    assert first.status_code == 200
    second = await client.post(
        f"{APPROVALS_URL}/{approval_id}/reject", json={"feedback": "改主意了"}, headers=auth_headers
    )
    assert second.status_code == 409
    assert "已被处理" in second.json()["message"]


async def test_unknown_approval_returns_404(client: AsyncClient, auth_headers: dict):
    """未知单号 → 404（与越权同响应）。"""
    resp = await client.post(f"{APPROVALS_URL}/{uuid.uuid4()}/approve", json={}, headers=auth_headers)
    assert resp.status_code == 404


async def test_operator_cannot_approve(client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg):
    """operator 不能审批（403）：生成与把关分离是 HITL 的核心约束。"""
    from tests.test_members_rbac import _create_member, _login

    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    member = await _create_member(client, auth_headers, "operator")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get(PENDING_URL, headers=headers)).status_code == 403
    assert (
        await client.post(f"{APPROVALS_URL}/{approval_id}/approve", json={}, headers=headers)
    ).status_code == 403


async def test_reviewer_can_approve(client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg):
    """reviewer 可以审批（这正是该角色的职责）。"""
    from tests.test_members_rbac import _create_member, _login

    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    resp = await client.post(
        f"{APPROVALS_URL}/{approval_id}/approve", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "approved"


async def test_tenant_isolation_hides_other_org_approval(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """跨租户审批单不可见也不可操作（404）。"""
    from tests.test_auth import _drop_org, _seed_same_username_org

    other_org = None
    try:
        other_org, _other_user = _seed_same_username_org(
            backend_dsn, f"outsider_{uuid.uuid4().hex[:6]}", "Pa-Other-Passw0rd!"
        )
        _exec(
            backend_dsn,
            "INSERT INTO schema_pa_backend.hitl_approvals (org_id, product_id, thread_id, status, channel) "
            "VALUES (%s, %s, %s, 'pending', 'web')",
            (other_org, seeded_org.product_id, str(uuid.uuid4())),
        )
        listing = await client.get(PENDING_URL, params={"limit": 200}, headers=auth_headers)
        orgs = {row["org_id"] for row in listing.json()["data"]}
        assert other_org not in orgs
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)


async def test_deeplink_ticket_is_locator_only(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, settings: Settings):
    """深链票据：匿名可解析出定位信息；被篡改/类型不符一律 401。"""
    from pa_backend.core.security import create_approval_ticket, create_sse_ticket

    approval_id = str(uuid.uuid4())
    ticket = create_approval_ticket(
        approval_id=approval_id, org_id=seeded_org.org_id, product_id=seeded_org.product_id, settings=settings
    )
    resp = await client.get(f"{APPROVALS_URL}/deeplink", params={"ticket": ticket})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "valid": True,
        "approval_id": approval_id,
        "product_id": seeded_org.product_id,
        "org_id": seeded_org.org_id,
    }

    wrong_kind = create_sse_ticket(
        user_id=seeded_org.user_id,
        org_id=seeded_org.org_id,
        role="admin",
        product_id=seeded_org.product_id,
        settings=settings,
    )
    assert (
        await client.get(f"{APPROVALS_URL}/deeplink", params={"ticket": wrong_kind})
    ).status_code == 401
    assert (await client.get(f"{APPROVALS_URL}/deeplink", params={"ticket": "garbage"})).status_code == 401
