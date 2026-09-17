"""运维手工终止任务（``POST /ops/jobs/{job_id}/abort``）用例。

为什么需要这个入口:
    卡在 ``running`` 的任务会让商品永久 409（前端按钮也禁用），过去只能改库且不留痕。
    自动兜底（reaper / 守卫宽容）要等阈值；本接口用于**立刻处置**并写 ``job_abort_audits`` 审计。

本文件锁定的红线:
    · 权限：只有 admin 能终止（运营/审核员 403）；
    · 审计**必须落库**（谁/何时/为什么），且审计表只增不改；
    · 越权与不存在同响应（404，不泄露存在性）；
    · 已终态任务 → 409（不做静默成功，避免运维误判「是我终止生效的」）；
    · 终止后商品必须回到可重试状态（否则等于没修）。
"""

from __future__ import annotations

from httpx import AsyncClient

from pa_backend.core.config import Settings
from tests.conftest import SeededOrg, _exec, seed_job

ABORT_URL = "/api/v1/ops/jobs/{job_id}/abort"
OVERVIEW_URL = "/api/v1/ops/overview"
GENERATE_URL = "/api/v1/products/{product_id}/generate"


def _job_id(backend_dsn: str, thread_id: str) -> str:
    """按 thread_id 取 job_id（abort 接口用的是 job 主键）。"""
    import psycopg

    with psycopg.connect(backend_dsn) as conn:
        row = conn.execute(
            "SELECT id FROM schema_pa_backend.generation_jobs WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _job_row(backend_dsn: str, thread_id: str) -> dict:
    """读任务行的（status, error）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(backend_dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, error FROM schema_pa_backend.generation_jobs WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    assert row is not None
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
    assert row is not None
    return dict(row)


def _abort_audits(backend_dsn: str, *, thread_id: str) -> list[dict]:
    """读某线程的终止审计（必须能查到，否则等于没有审计）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(backend_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT job_id, thread_id, product_id, actor_user_id, reason "
            "FROM schema_pa_backend.job_abort_audits WHERE thread_id = %s ORDER BY aborted_at DESC",
            (thread_id,),
        ).fetchall()
    return [dict(row) for row in rows]


async def test_abort_releases_product_and_writes_audit(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """admin 终止卡死任务：job→failed、商品→draft、审计落库（含操作人与原因）。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    job_id = _job_id(backend_dsn, thread_id)

    resp = await client.post(
        ABORT_URL.format(job_id=job_id), headers=auth_headers, json={"reason": "worker 已崩溃，人工终止"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["job_status"] == "failed"
    assert data["product_released"] is True
    assert data["already_terminal"] is False

    job = _job_row(backend_dsn, thread_id)
    assert job["status"] == "failed"
    assert "人工终止" in (job["error"] or "")
    product = _product_row(backend_dsn, seeded_org.product_id)
    assert product["status"] == "draft"
    assert product["active_thread_id"] is None

    audits = _abort_audits(backend_dsn, thread_id=thread_id)
    assert len(audits) == 1
    assert audits[0]["reason"] == "worker 已崩溃，人工终止"
    assert str(audits[0]["actor_user_id"]) == seeded_org.user_id
    assert str(audits[0]["product_id"]) == seeded_org.product_id

    # 终止后可立刻重新生成（这是「终止」的意义所在）
    again = await client.post(
        GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["thread_id"] != thread_id


async def test_abort_requires_admin(
    client: AsyncClient, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """非 admin（运营/审核员）不能终止：403；且**不得**产生任何副作用。

    注意：角色取自**库里的 sys_users.role**（``deps.get_current_user`` 每请求查库），
    不是 JWT claim —— 所以这里必须造一个真正的 operator 用户，而不是改 claim。
    """
    import uuid as _uuid

    from pa_backend.core.security import create_access_token, hash_password

    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    job_id = _job_id(backend_dsn, thread_id)

    operator_id = str(_uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users "
        "(id, org_id, username, hashed_password, role, status) VALUES (%s, %s, %s, %s, 'operator', 'active')",
        (operator_id, seeded_org.org_id, f"operator_{operator_id[:8]}", hash_password("Pa-Test-Passw0rd!")),
    )
    token = create_access_token(
        user_id=operator_id, org_id=seeded_org.org_id, role="operator", settings=settings
    )
    resp = await client.post(
        ABORT_URL.format(job_id=job_id),
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "越权尝试"},
    )
    assert resp.status_code == 403, resp.text
    assert _job_row(backend_dsn, thread_id)["status"] == "running"
    assert _abort_audits(backend_dsn, thread_id=thread_id) == []


async def test_abort_rejects_terminal_job_with_409(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """已终态任务 → 409（不静默成功：运维必须知道「终止没生效」），且不写审计。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="succeeded",
        product_status="published",
    )
    job_id = _job_id(backend_dsn, thread_id)
    resp = await client.post(
        ABORT_URL.format(job_id=job_id), headers=auth_headers, json={"reason": "重复终止"}
    )
    assert resp.status_code == 409, resp.text
    assert _abort_audits(backend_dsn, thread_id=thread_id) == []


async def test_abort_requires_reason(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """原因为空 → 422（pydantic min_length）或 400；无论如何**不得**改状态、不得写审计。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    job_id = _job_id(backend_dsn, thread_id)
    resp = await client.post(
        ABORT_URL.format(job_id=job_id), headers=auth_headers, json={"reason": ""}
    )
    assert resp.status_code in (400, 422), resp.text
    assert _job_row(backend_dsn, thread_id)["status"] == "running"
    assert _abort_audits(backend_dsn, thread_id=thread_id) == []


async def test_abort_other_tenant_job_is_404(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """跨租户任务 → 404（越权与不存在同响应，不泄露存在性）。"""
    import uuid as _uuid

    other_org = str(_uuid.uuid4())
    job_id = str(_uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, '别的租户', 'active')",
        (other_org,),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.generation_jobs (id, thread_id, org_id, product_id, status) "
        "VALUES (%s, gen_random_uuid(), %s, %s, 'running')",
        (job_id, other_org, seeded_org.product_id),
    )
    try:
        resp = await client.post(
            ABORT_URL.format(job_id=job_id), headers=auth_headers, json={"reason": "越租户"}
        )
        assert resp.status_code == 404, resp.text
        assert _job_row_by_id(backend_dsn, job_id)["status"] == "running"
    finally:
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.generation_jobs WHERE org_id = %s", (other_org,))
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.organizations WHERE id = %s", (other_org,))


def _job_row_by_id(backend_dsn: str, job_id: str) -> dict:
    """按 job 主键读任务状态（跨租户用例用）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(backend_dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM schema_pa_backend.generation_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


async def test_overview_lists_stuck_jobs(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """运维面板能看到「卡住任务」（判据与 reaper 同源）→ 才有终止的入口。"""
    from pa_backend.services.generation_job_reaper import DEFAULT_STALE_MINUTES

    stale_thread = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        age_minutes=DEFAULT_STALE_MINUTES + 5,
    )
    resp = await client.get(OVERVIEW_URL, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    stuck = resp.json()["data"]["stuck_jobs"]
    assert [item["thread_id"] for item in stuck] == [stale_thread]
    assert stuck[0]["job_status"] == "running"
    assert stuck[0]["sku_code"]
    assert stuck[0]["age_seconds"] >= (DEFAULT_STALE_MINUTES + 4) * 60
