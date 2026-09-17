"""商品 CRUD 用例：列表分页 / 新建 / 更新 / 历史已删行的可见性 / 越权 / RBAC。

覆盖的边界（都是「错了会静默出问题」的点）:
    · ``raw_images`` 响应形状被规范化成字符串数组（前端不必兼容 jsonb 的两种历史形状）；
    · **删除只有彻底删除一条路径**：`DELETE /products/{id}`（原软删）已下线并显式返回 405；
    · ``status='deleted'`` 的行只可能是**历史数据**：默认列表隐藏、``?status=deleted`` 可查
      （便于发现与清理）、且不可修改；
    · 跨租户访问一律 404（不泄露实体是否存在）；
    · ``reviewer`` 只能读，不能写（RBAC）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import SeededOrg, _exec

PRODUCTS_URL = "/api/v1/products"


async def _create_product(client: AsyncClient, headers: dict, **overrides) -> dict:
    """经接口建一个商品并返回响应体（SKU 随机，避免跨用例冲突）。"""
    payload = {
        "sku_code": f"SKU-{uuid.uuid4().hex[:10]}",
        "title": "接口创建商品·保温杯",
        "base_price": 199.5,
        "stock_status": "in_stock",
        "raw_images": [],
    }
    payload.update(overrides)
    resp = await client.post(PRODUCTS_URL, json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_create_and_get_product(client: AsyncClient, auth_headers: dict):
    """新建商品：状态 draft、字段原样回读、详情附 ``active_job_status=None``。"""
    created = await _create_product(client, auth_headers, raw_images=["https://example.test/a.png"])
    assert created["status"] == "draft"
    assert created["raw_images"] == ["https://example.test/a.png"]

    detail = await client.get(f"{PRODUCTS_URL}/{created['id']}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()["data"]
    assert body["sku_code"] == created["sku_code"]
    assert body["active_job_status"] is None  # 还没触发过生成


async def test_detail_exposes_last_failure_reason_after_terminal(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """失败原因在任务**终态之后**仍要能看到（否则界面只剩「商品莫名回到 draft」）。

    实测场景（2026-09）: 标题命中违禁词 → 任务 failed、``active_thread_id`` 被清空 →
    若详情只读「进行中任务」，``active_job_error`` 恒空 —— 合规拦截在界面上完全不可见。
    这里锁定「回退到最近一条任务」的行为。
    """
    from tests.conftest import seed_job

    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="failed",
        product_status="draft",
    )
    reason = "input_compliance_blocked(商品标题): 命中违禁词「最便宜」（广告法种子）"
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.generation_jobs SET error = %s WHERE thread_id = %s",
        (reason, thread_id),
    )

    detail = await client.get(f"{PRODUCTS_URL}/{seeded_org.product_id}", headers=auth_headers)

    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["active_job_status"] is None, "已终态：没有进行中任务"
    assert body["active_job_error"] == reason


async def test_raw_images_is_normalized_to_string_array(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """历史 ``[{url: …}]`` 形状在响应里被规范化为字符串数组（前端只处理一种形状）。"""
    from tests.conftest import _exec

    created = await _create_product(client, auth_headers)
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.products SET raw_images = %s::jsonb WHERE id = %s",
        ('[{"url": "https://example.test/legacy.png"}]', created["id"]),
    )
    resp = await client.get(f"{PRODUCTS_URL}/{created['id']}", headers=auth_headers)
    assert resp.json()["data"]["raw_images"] == ["https://example.test/legacy.png"]


async def test_duplicate_sku_returns_409(client: AsyncClient, auth_headers: dict):
    """同租户内 SKU 唯一（``uq_products_org_sku``）→ 409。"""
    created = await _create_product(client, auth_headers)
    resp = await client.post(
        PRODUCTS_URL,
        json={"sku_code": created["sku_code"], "title": "重复 SKU", "base_price": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_list_supports_pagination_and_total_header(client: AsyncClient, auth_headers: dict, seeded_org):
    """列表：``X-Total-Count`` 给出总数，``limit`` 生效（响应体仍是数组）。"""
    for _ in range(2):
        await _create_product(client, auth_headers)
    resp = await client.get(PRODUCTS_URL, params={"limit": 1}, headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1
    assert int(resp.headers["X-Total-Count"]) >= 3  # 夹具 1 个 + 本用例 2 个


async def test_patch_updates_whitelisted_fields(client: AsyncClient, auth_headers: dict):
    """PATCH 只改提供的字段；非法库存枚举在入口被拒（400）。"""
    created = await _create_product(client, auth_headers)
    resp = await client.patch(
        f"{PRODUCTS_URL}/{created['id']}",
        json={"title": "改过的标题", "stock_status": "preorder"},  # preorder 是 PA 独有枚举
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["title"] == "改过的标题"
    assert body["stock_status"] == "preorder"
    assert body["base_price"] == created["base_price"]  # 未提供的字段不动

    bad = await client.patch(
        f"{PRODUCTS_URL}/{created['id']}", json={"stock_status": "有货"}, headers=auth_headers
    )
    assert bad.status_code == 400


async def test_soft_delete_endpoint_is_retired(client: AsyncClient, auth_headers: dict):
    """软删端点已下线：``DELETE /products/{id}`` → 405（信封 + 可读原因），商品不受影响。"""
    created = await _create_product(client, auth_headers)
    resp = await client.delete(f"{PRODUCTS_URL}/{created['id']}", headers=auth_headers)
    assert resp.status_code == 405
    body = resp.json()
    assert body["code"] == 405 and body["data"] is None
    assert "彻底删除" in body["message"]
    # 商品仍可正常读取（软删没有偷跑）
    still = await client.get(f"{PRODUCTS_URL}/{created['id']}", headers=auth_headers)
    assert still.status_code == 200
    assert still.json()["data"]["status"] == "draft"


async def test_legacy_deleted_rows_hidden_but_queryable_and_readonly(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str
):
    """历史 ``deleted`` 行：默认列表隐藏、``?status=deleted`` 可查、不可修改。

    为什么用 SQL 直接造这一行:
        软删端点已下线 → 接口层再也造不出 ``deleted`` 状态；但历史库/备份恢复的数据里可能有，
        因此「隐藏 + 可查 + 只读」这三条仍然必须成立（否则历史行会变成「看不见也改不了、只能手改库」）。
    """
    legacy_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.products "
        "(id, org_id, owner_id, sku_code, title, base_price, stock_status, status, raw_images) "
        "VALUES (%s, %s, %s, %s, '历史已删商品', 10.00, 'in_stock', 'deleted', '[]'::jsonb)",
        (legacy_id, seeded_org.org_id, seeded_org.user_id, f"SKU-LEGACY-{legacy_id[:8]}"),
    )
    try:
        default_list = await client.get(PRODUCTS_URL, params={"limit": 200}, headers=auth_headers)
        assert legacy_id not in {item["id"] for item in default_list.json()["data"]}

        deleted_list = await client.get(
            PRODUCTS_URL, params={"status": "deleted", "limit": 200}, headers=auth_headers
        )
        assert legacy_id in {item["id"] for item in deleted_list.json()["data"]}

        # 历史已删行不可修改（避免「删了还能改」的语义混乱）
        patch = await client.patch(f"{PRODUCTS_URL}/{legacy_id}", json={"title": "x"}, headers=auth_headers)
        assert patch.status_code == 409
    finally:
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE id = %s", (legacy_id,))


async def test_unknown_and_malformed_ids_return_404(client: AsyncClient, auth_headers: dict):
    """未知 id 与非法 id 都是 404（同响应，不给探测信号）。"""
    assert (await client.get(f"{PRODUCTS_URL}/{uuid.uuid4()}", headers=auth_headers)).status_code == 404
    assert (await client.get(f"{PRODUCTS_URL}/not-a-uuid", headers=auth_headers)).status_code == 404


async def test_tenant_isolation(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str):
    """跨租户不可见/不可改（查询层强制带 ``org_id``）。"""
    from pa_backend.core.config import Settings
    from pa_backend.core.security import create_access_token
    from tests.test_auth import _drop_org, _seed_same_username_org

    other_org = None
    try:
        other_org, other_user_id = _seed_same_username_org(
            backend_dsn, f"other_{uuid.uuid4().hex[:6]}", "Pa-Other-Passw0rd!"
        )
        other_token = create_access_token(
            user_id=other_user_id, org_id=other_org, role="admin", settings=Settings()
        )
        resp = await client.get(
            f"{PRODUCTS_URL}/{seeded_org.product_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 404
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)


async def test_reviewer_cannot_write_but_can_read(client: AsyncClient, auth_headers: dict):
    """RBAC：reviewer 可读列表，但创建/删除一律 403。"""
    from tests.test_members_rbac import _create_member, _login

    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get(PRODUCTS_URL, headers=headers)).status_code == 200
    assert (
        await client.post(
            PRODUCTS_URL, json={"sku_code": "SKU-RV", "title": "reviewer 不许建"}, headers=headers
        )
    ).status_code == 403
    created = await _create_product(client, auth_headers)
    assert (await client.delete(f"{PRODUCTS_URL}/{created['id']}", headers=headers)).status_code == 403
