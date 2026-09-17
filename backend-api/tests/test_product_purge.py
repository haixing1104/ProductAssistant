"""彻底删除（purge）用例：审计留痕 + 物理删行 + AI 域清理投递。

最重要的两条契约（都从 ai-engine 代码里核对过，改前请先看 ``services/product_service.purge``）:
    ① ``job:product_purge`` 的**前置守卫**：ai-engine 若发现 ``products`` 行仍在，会中止清理并
       ack 丢弃 —— 所以 backend 必须「先删行 + commit，再投递」；
    ② ``role_pa_backend`` 对 ``delete_audits`` 只有 SELECT/INSERT（0004 REVOKE UPDATE/DELETE）：
       本用例用「审计行存在且内容正确」证明写审计这条路是通的，而不是靠代码自觉。
"""

from __future__ import annotations

import uuid

import psycopg
from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.ai_engine_client import AIEngineClient
from pa_backend.services.event_envelope import decode_fields
from tests.conftest import SeededOrg, _exec

PRODUCTS_URL = "/api/v1/products"
PURGE_URL = "/api/v1/products/{product_id}/purge"


def _seed_children(backend_dsn: str, *, org_id: str, product_id: str) -> str:
    """给商品造子表数据（job + 待审记录），返回 thread_id。

    为什么要造：``generation_jobs`` / ``hitl_approvals`` 对 ``products.id`` 有物理外键且**未定义级联**，
    若 hard delete 时漏删子表，数据库会直接拒绝 —— 用例把这条约束变成可回归的断言。
    """
    thread_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.generation_jobs (thread_id, org_id, product_id, status) "
        "VALUES (%s, %s, %s, 'waiting_input')",
        (thread_id, org_id, product_id),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals (org_id, product_id, thread_id, status, channel) "
        "VALUES (%s, %s, %s, 'pending', 'web')",
        (org_id, product_id, thread_id),
    )
    return thread_id


def _read_purge_messages(settings: Settings) -> list[dict]:
    """读取 ``job:product_purge`` 流里的载荷。"""
    client = new_sync_redis(settings)
    try:
        entries = client.xrange(RedisKeys(settings.env).job_product_purge(), min="-", max="+", count=200)
        return [decode_fields(fields) for _id, fields in entries]
    finally:
        client.close()


def _drain_purge_stream(settings: Settings) -> None:
    """清空 purge 流（断言只看本次投递）。"""
    client = new_sync_redis(settings)
    try:
        client.delete(RedisKeys(settings.env).job_product_purge())
    finally:
        client.close()


async def test_purge_requires_reason(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """缺 reason → 400（审计要求可追溯）。"""
    resp = await client.request(
        "DELETE", PURGE_URL.format(product_id=seeded_org.product_id), json={}, headers=auth_headers
    )
    assert resp.status_code == 400


async def test_purge_writes_audit_deletes_rows_and_enqueues_cleanup(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str, settings: Settings
):
    """purge：审计落库 + 商品与子表物理删除 + 投递清理消息（信封含 schema_version）。"""
    _seed_children(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    _drain_purge_stream(settings)

    resp = await client.request(
        "DELETE",
        PURGE_URL.format(product_id=seeded_org.product_id),
        json={"reason": "运营确认不再上架"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["product_id"] == seeded_org.product_id
    assert data["purge_enqueued"] is True

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        # ① 审计行存在且内容正确（role_pa_backend 可 SELECT/INSERT，但不可 UPDATE/DELETE）
        audit = conn.execute(
            "SELECT sku_code, title, reason FROM schema_pa_backend.delete_audits WHERE product_id = %s",
            (seeded_org.product_id,),
        ).fetchall()
        assert len(audit) == 1
        assert audit[0][2] == "运营确认不再上架"
        # ② 商品与子表都不在了（FK 约束下的物理删除）
        assert (
            conn.execute(
                "SELECT count(*) FROM schema_pa_backend.products WHERE id = %s", (seeded_org.product_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM schema_pa_backend.generation_jobs WHERE product_id = %s",
                (seeded_org.product_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM schema_pa_backend.hitl_approvals WHERE product_id = %s",
                (seeded_org.product_id,),
            ).fetchone()[0]
            == 0
        )

    # ③ 清理消息已投递，且载荷与 ai-engine 的 reader 对齐
    messages = _read_purge_messages(settings)
    assert len(messages) == 1
    assert messages[0]["schema_version"] == 1
    assert messages[0]["product_id"] == seeded_org.product_id
    assert messages[0]["org_id"] == seeded_org.org_id


async def test_purge_reports_enqueue_failure_but_keeps_deletion(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str, monkeypatch
):
    """Redis 不可用时：删除仍然完成（审计已落库），但 ``purge_enqueued=False`` 必须回传。"""

    def _boom(self, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(AIEngineClient, "trigger_product_purge", _boom)
    resp = await client.request(
        "DELETE",
        PURGE_URL.format(product_id=seeded_org.product_id),
        json={"reason": "Redis 故障场景"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["purge_enqueued"] is False

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM schema_pa_backend.products WHERE id = %s", (seeded_org.product_id,)
            ).fetchone()[0]
            == 0
        )


async def test_purge_unknown_product_returns_404(client: AsyncClient, auth_headers: dict):
    """未知商品 → 404（不泄露存在性）。"""
    resp = await client.request(
        "DELETE", PURGE_URL.format(product_id=uuid.uuid4()), json={"reason": "x"}, headers=auth_headers
    )
    assert resp.status_code == 404


async def test_purge_requires_admin(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """reviewer 不能彻底删除（403）—— 物理删除必须收口到 admin。"""
    from tests.test_members_rbac import _create_member, _login

    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    resp = await client.request(
        "DELETE",
        PURGE_URL.format(product_id=seeded_org.product_id),
        json={"reason": "越权尝试"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_purge_twice_second_call_is_404(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg
):
    """幂等边界：第二次 purge 返回 404（行已不存在），而不是 500 或「假装成功」。"""
    first = await client.request(
        "DELETE",
        PURGE_URL.format(product_id=seeded_org.product_id),
        json={"reason": "第一次"},
        headers=auth_headers,
    )
    assert first.status_code == 200
    second = await client.request(
        "DELETE",
        PURGE_URL.format(product_id=seeded_org.product_id),
        json={"reason": "第二次"},
        headers=auth_headers,
    )
    assert second.status_code == 404
