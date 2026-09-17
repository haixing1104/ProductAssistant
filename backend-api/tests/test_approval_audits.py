"""审批审计用例：补投语义（W4）与人工放行留痕（W6）。

为什么要有这组用例（2026-09 实测）:
    · 补投按钮此前"点了看不到任何变化"：对已跑完的线程再 resume 是**静默 no-op**
      （实测：不报错、不重跑、不重复落库、不翻转决策），而接口一律回 ``resume_enqueued=true``
      —— 于是"引擎已收到"与"真的投出去了"在界面上完全无法区分；
    · 合规命中转人工后，审批人一点「批准」即可上架：既没有强制说明，也没有留痕，
      事后无法回答"谁在知情下放行了哪条命中点"。

本文件的断言锁住两件事：**不假装补投**（not_needed/throttled 都是显式结果）+ **放行必有痕**。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import SeededOrg, _exec, seed_job
from tests.test_approvals import _drain_approval_stream, _read_approval_messages, _seed_pending_approval

APPROVALS_URL = "/api/v1/approvals"

#: 带「评估命中点」的待审快照（W6 的门槛判据取 evaluation_result.violations）
VIOLATION_SNAPSHOT = (
    '{"reason": "quality_exhausted", '
    '"content": {"blocks": [{"type": "text", "text": "全网最便宜的水杯"}]}, '
    '"evaluation_result": {"passed": false, "score": 60.0, '
    '"violations": [{"keyword": "最便宜", "reason": "命中违禁词「最便宜」（广告法种子）", '
    '"rule_id": "w-1", "severity": "high"}], "facts_checked": []}, '
    '"evaluation_attempts": 3}'
)


def _set_product_status(dsn: str, product_id: str, status: str) -> None:
    """直接改商品状态（模拟 ai-engine resume 后的终态推进）。"""
    _exec(dsn, "UPDATE schema_pa_backend.products SET status = %s WHERE id = %s", (status, product_id))


def _seed_approval_with_violations(dsn: str, *, org_id: str, product_id: str) -> tuple[str, str]:
    """造一条「带命中点」的待审单，返回 (thread_id, approval_id)。"""
    thread_id = seed_job(
        dsn, org_id=org_id, product_id=product_id, job_status="waiting_input", product_status="waiting_approval"
    )
    approval_id = str(uuid.uuid4())
    _exec(
        dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals "
        "(id, org_id, product_id, thread_id, status, channel, content_snapshot) "
        "VALUES (%s, %s, %s, %s, 'pending', 'web', %s::jsonb)",
        (approval_id, org_id, product_id, thread_id, VIOLATION_SNAPSHOT),
    )
    return thread_id, approval_id


def _redrive_audits(dsn: str, approval_id: str) -> list[tuple[str, str | None]]:
    """读补投审计（outcome, reason，新→旧）。"""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT outcome, reason FROM schema_pa_backend.approval_redrive_audits "
            "WHERE approval_id = %s ORDER BY redriven_at DESC",
            (approval_id,),
        ).fetchall()


def _override_rows(dsn: str, approval_id: str) -> list[tuple]:
    """读人工放行审计（violation_count, violations, reason）。"""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT violation_count, violations, reason FROM schema_pa_backend.approval_overrides "
            "WHERE approval_id = %s ORDER BY created_at DESC",
            (approval_id,),
        ).fetchall()


# ------------------------------------------------------------ W4：补投语义
async def test_redrive_not_needed_when_engine_already_consumed(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings
):
    """商品已推进（引擎已消费）→ 不投递、回 not_needed，并留下审计。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    rejected = await client.post(f"{APPROVALS_URL}/{approval_id}/reject", json={"feedback": "x"}, headers=auth_headers)
    assert rejected.status_code == 200, rejected.text
    _set_product_status(backend_dsn, seeded_org.product_id, "draft")  # 模拟 ai-engine 已把商品回 draft

    _drain_approval_stream(settings)
    resp = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["needed"] is False
    assert body["outcome"] == "not_needed"
    assert body["resume_enqueued"] is False
    assert _read_approval_messages(settings) == [], "不需要补投时不得投递（否则只是噪声）"
    assert _redrive_audits(backend_dsn, approval_id)[0][0] == "not_needed"


async def test_redrive_enqueues_when_product_still_waiting(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings
):
    """商品仍停在 waiting_approval（引擎很可能没收到）→ 真投递 + 审计。"""
    thread_id, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    rejected = await client.post(f"{APPROVALS_URL}/{approval_id}/reject", json={"feedback": "x"}, headers=auth_headers)
    assert rejected.status_code == 200, rejected.text
    # 商品仍是 waiting_approval（backend 不推进它；resume 的终态才由 ai-engine 决定）

    _drain_approval_stream(settings)
    resp = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["needed"] is True and body["outcome"] == "enqueued" and body["resume_enqueued"] is True
    messages = _read_approval_messages(settings)
    assert [m["thread_id"] for m in messages] == [thread_id]
    assert messages[0]["result"] == "rejected"
    assert _redrive_audits(backend_dsn, approval_id)[0][0] == "enqueued"


async def test_redrive_throttled_on_second_call(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings
):
    """同一张单在节流窗口内重复补投 → throttled，且**不会重复投递**。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    await client.post(f"{APPROVALS_URL}/{approval_id}/reject", json={"feedback": "x"}, headers=auth_headers)

    _drain_approval_stream(settings)
    first = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)
    second = await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)

    assert first.json()["data"]["outcome"] == "enqueued"
    assert second.status_code == 200, second.text
    assert second.json()["data"]["outcome"] == "throttled"
    assert second.json()["data"]["resume_enqueued"] is False
    assert len(_read_approval_messages(settings)) == 1, "节流必须真的挡住第二次投递"
    outcomes = [row[0] for row in _redrive_audits(backend_dsn, approval_id)]
    assert outcomes[0] == "throttled" and "enqueued" in outcomes


async def test_redrive_history_visible_in_detail(client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg):
    """补投痕迹必须能回显（否则界面上永远说不清"到底投没投"）。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    await client.post(f"{APPROVALS_URL}/{approval_id}/reject", json={"feedback": "x"}, headers=auth_headers)
    await client.post(f"{APPROVALS_URL}/{approval_id}/redrive", headers=auth_headers)

    detail = await client.get(f"{APPROVALS_URL}/{approval_id}", headers=auth_headers)

    assert detail.status_code == 200, detail.text
    redrive = detail.json()["data"]["redrive"]
    assert redrive is not None and redrive["count"] >= 1
    assert redrive["last_outcome"] in {"enqueued", "not_needed", "throttled", "enqueue_failed"}
    assert redrive["last_at"]


# ------------------------------------------------------------ W6：放行留痕
async def test_approve_with_violations_requires_reason_and_audits(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """带评估命中点的单：批准必须写理由；放行后审计表留下命中点快照与理由。"""
    _thread, approval_id = _seed_approval_with_violations(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )

    bare = await client.post(f"{APPROVALS_URL}/{approval_id}/approve", json={"feedback": None}, headers=auth_headers)
    assert bare.status_code == 422, bare.text
    assert "命中点" in bare.json()["message"]
    assert _override_rows(backend_dsn, approval_id) == [], "被拒的批准请求不得留下放行审计"

    ok_resp = await client.post(
        f"{APPROVALS_URL}/{approval_id}/approve",
        json={"feedback": "已核对：标题将同步整改，本次先放行"},
        headers=auth_headers,
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["data"]["override"] is True

    rows = _override_rows(backend_dsn, approval_id)
    assert len(rows) == 1
    count, violations, reason = rows[0]
    assert count == 1 and violations[0]["keyword"] == "最便宜"
    assert reason == "已核对：标题将同步整改，本次先放行"

    detail = await client.get(f"{APPROVALS_URL}/{approval_id}", headers=auth_headers)
    override = detail.json()["data"]["override"]
    assert override is not None and override["violation_count"] == 1


async def test_approve_without_violations_unchanged(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """无命中点的单：不带理由也能批准（不改变既有行为），且不产生放行审计。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )

    resp = await client.post(f"{APPROVALS_URL}/{approval_id}/approve", json={"feedback": None}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["override"] is False
    assert _override_rows(backend_dsn, approval_id) == []


