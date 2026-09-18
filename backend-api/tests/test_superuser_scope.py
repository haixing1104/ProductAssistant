"""平台超管跨租户用例（``X-Org-Id`` 覆盖）—— 本文件是「隔离红线没被放宽」的安全网。

背景（见 ``core/deps`` 模块 docstring）:
    超管的租户覆盖只发生在 ``get_current_user`` 一处，因此**写路径 / 审批 CAS / 生成投递
    全部沿用它**。这带来两种必须锁住的风险:
      ① **放宽过头**：普通 admin 也能带 ``X-Org-Id`` 跨租户 → 全量数据泄露（红线）；
      ② **静默失败**：超管带了头却没生效（头名写错、目标校验写反），表现为「界面选完组织
         还是旧租户的数据」—— 比报错更难发现，故用「能读到 B / 读不到 A」双向断言。

夹具自给自足（不改 ``conftest``）: ``two_orgs`` 造两个业务租户（各含 admin + draft 商品）、
一个平台超管（第三个组织）与一个**已停用**组织（用于 400 分支）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.deps import SUPERUSER_ORG_HEADER
from pa_backend.core.security import create_access_token, hash_password
from tests.conftest import _exec

ME_URL = "/api/v1/auth/me"
ORGS_URL = "/api/v1/orgs"
PRODUCTS_URL = "/api/v1/products"

#: 超管的归属组织里**不放商品** —— 这样「无头 → 看到 0 个商品」本身就证明没有跨租户
EMPTY_ORG_MARKER = "平台组织"


@dataclass(frozen=True)
class _Tenant:
    """一个最小业务租户（组织 + admin + 一个 draft 商品）。"""

    org_id: str
    user_id: str
    product_id: str
    username: str


@dataclass(frozen=True)
class TwoOrgs:
    """``two_orgs`` 夹具的产出。"""

    tenant_a: _Tenant
    tenant_b: _Tenant
    super_org_id: str
    super_user_id: str
    super_username: str
    super_password: str
    suspended_org_id: str


def _seed_tenant(backend_dsn: str, tag: str) -> _Tenant:
    """造一个业务租户：组织 + admin + 一个 draft 商品。

    参数:
        backend_dsn: ``role_pa_backend`` DSN（业务域表只有 backend 能写）。
        tag: 组织名前缀（A / B），便于失败信息定位。
    返回:
        ``_Tenant``。
    """
    org_id, user_id, product_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    username = f"admin{tag}_{user_id[:8]}"
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, %s, 'active')",
        (org_id, f"超管用例组织{tag}-{org_id[:8]}"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users "
        "(id, org_id, username, hashed_password, role, status) VALUES (%s, %s, %s, %s, 'admin', 'active')",
        (user_id, org_id, username, hash_password("Pa-Test-Passw0rd!")),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.products "
        "(id, org_id, sku_code, title, base_price, stock_status, status, owner_id, raw_images) "
        "VALUES (%s, %s, %s, %s, %s, 'in_stock', 'draft', %s, '[]'::jsonb)",
        (product_id, org_id, f"SKU{tag}-{product_id[:8]}", f"跨租户用例商品·{tag}", 199.0, user_id),
    )
    return _Tenant(org_id=org_id, user_id=user_id, product_id=product_id, username=username)


def _cleanup_org(backend_dsn: str, org_id: str) -> None:
    """逆序清理一个组织的全部测试数据（先子表后父表）。

    注意:
        ``delete_audits`` 刻意不清理 —— ``0004`` 显式 REVOKE 了 UPDATE/DELETE，
        审计表只能追加（见 ``test_roles_red_lines``）。
    """
    for table in ("generation_jobs", "hitl_approvals", "notification_outbox"):
        _exec(backend_dsn, f"DELETE FROM schema_pa_backend.{table} WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.sys_users WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.organizations WHERE id = %s", (org_id,))


@pytest.fixture
def two_orgs(backend_dsn: str):
    """两个业务租户 + 一个平台超管 + 一个已停用组织（用完即清）。"""
    tenant_a = _seed_tenant(backend_dsn, "A")
    tenant_b = _seed_tenant(backend_dsn, "B")
    super_org_id, super_user_id = str(uuid.uuid4()), str(uuid.uuid4())
    super_username, super_password = f"super_{super_user_id[:8]}", "Pa-Super-Passw0rd!"
    suspended_org_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, %s, 'active')",
        (super_org_id, f"{EMPTY_ORG_MARKER}-{super_org_id[:8]}"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users "
        "(id, org_id, username, hashed_password, role, status, is_superuser) "
        "VALUES (%s, %s, %s, %s, 'admin', 'active', true)",
        (super_user_id, super_org_id, super_username, hash_password(super_password)),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, %s, 'suspended')",
        (suspended_org_id, f"超管用例停用组织-{suspended_org_id[:8]}"),
    )
    data = TwoOrgs(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        super_org_id=super_org_id,
        super_user_id=super_user_id,
        super_username=super_username,
        super_password=super_password,
        suspended_org_id=suspended_org_id,
    )
    yield data
    for org_id in (tenant_a.org_id, tenant_b.org_id, super_org_id, suspended_org_id):
        _cleanup_org(backend_dsn, org_id)


def _headers(user_id: str, org_id: str, role: str = "admin") -> dict[str, str]:
    """签发 access token（跳过登录流程，聚焦租户覆盖本身）。

    参数:
        user_id: 用户 ID（写入 ``sub``）。
        org_id: 归属组织（写入 claims；超管不带头时它就是「当前租户」）。
        role: 角色（与服务端一致即可）。
    返回:
        含 ``Authorization`` 的请求头字典（需要 ``X-Org-Id`` 时由调用方追加）。
    """
    token = create_access_token(user_id=user_id, org_id=org_id, role=role, settings=Settings())
    return {"Authorization": f"Bearer {token}"}


def _product_ids(payload: dict) -> set[str]:
    """从列表响应体（``{"code":200,"data":[...]}``）取出商品 id 集合。"""
    return {item["id"] for item in payload["data"]}


async def test_me_reports_superuser_flag(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """``/auth/me`` 暴露 ``is_superuser``（前端据此显示组织选择器）；普通 admin 恒为 false。"""
    super_me = await client.get(
        ME_URL, headers=_headers(two_orgs.super_user_id, two_orgs.super_org_id)
    )
    assert super_me.status_code == 200, super_me.text
    assert super_me.json()["data"]["is_superuser"] is True

    plain_me = await client.get(
        ME_URL, headers=_headers(two_orgs.tenant_a.user_id, two_orgs.tenant_a.org_id)
    )
    assert plain_me.status_code == 200, plain_me.text
    assert plain_me.json()["data"]["is_superuser"] is False


async def test_orgs_scope_differs_by_superuser(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """``GET /orgs``：超管拿全量；普通 admin 只拿自己那一个（不能借列组织探测别的租户）。"""
    sup = await client.get(
        ORGS_URL, headers=_headers(two_orgs.super_user_id, two_orgs.super_org_id)
    )
    assert sup.status_code == 200, sup.text
    ids = {row["id"] for row in sup.json()["data"]}
    assert {two_orgs.tenant_a.org_id, two_orgs.tenant_b.org_id, two_orgs.super_org_id} <= ids

    plain = await client.get(
        ORGS_URL, headers=_headers(two_orgs.tenant_a.user_id, two_orgs.tenant_a.org_id)
    )
    assert plain.status_code == 200, plain.text
    assert [row["id"] for row in plain.json()["data"]] == [two_orgs.tenant_a.org_id]


async def test_plain_admin_header_is_ignored(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """红线：普通 admin 带 ``X-Org-Id`` 一律无效（静默忽略 + 仍被隔离在自己租户内）。"""
    headers = _headers(two_orgs.tenant_a.user_id, two_orgs.tenant_a.org_id)
    headers[SUPERUSER_ORG_HEADER] = two_orgs.tenant_b.org_id
    hit = await client.get(f"{PRODUCTS_URL}/{two_orgs.tenant_b.product_id}", headers=headers)
    assert hit.status_code == 404, hit.text
    listed = await client.get(PRODUCTS_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert _product_ids(listed.json()) == {two_orgs.tenant_a.product_id}


async def test_superuser_without_header_stays_in_own_org(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """超管不带头时行为与普通用户一致（= 归属组织）：覆盖必须**显式**发生，绝不默认放大。"""
    headers = _headers(two_orgs.super_user_id, two_orgs.super_org_id)
    listed = await client.get(PRODUCTS_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert _product_ids(listed.json()) == set()  # 平台组织内不放商品（夹具保证）
    other = await client.get(f"{PRODUCTS_URL}/{two_orgs.tenant_b.product_id}", headers=headers)
    assert other.status_code == 404, other.text


async def test_superuser_switches_tenant_by_header(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """超管带 ``X-Org-Id``：读得到目标租户，**读不到**未选中租户（双向断言防「静默没生效」）。"""
    headers = _headers(two_orgs.super_user_id, two_orgs.super_org_id)
    headers[SUPERUSER_ORG_HEADER] = two_orgs.tenant_b.org_id
    hit = await client.get(f"{PRODUCTS_URL}/{two_orgs.tenant_b.product_id}", headers=headers)
    assert hit.status_code == 200, hit.text
    listed = await client.get(PRODUCTS_URL, headers=headers)
    assert _product_ids(listed.json()) == {two_orgs.tenant_b.product_id}
    miss = await client.get(f"{PRODUCTS_URL}/{two_orgs.tenant_a.product_id}", headers=headers)
    assert miss.status_code == 404, miss.text


async def test_superuser_write_lands_in_target_org(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """写路径随覆盖落到目标租户：新建商品的 ``org_id`` 必须是目标组织（不是超管归属组织）。"""
    headers = _headers(two_orgs.super_user_id, two_orgs.super_org_id)
    headers[SUPERUSER_ORG_HEADER] = two_orgs.tenant_b.org_id
    created = await client.post(
        PRODUCTS_URL,
        headers=headers,
        json={"sku_code": f"SUP-{uuid.uuid4().hex[:8]}", "title": "超管在 B 租户建的商品"},
    )
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    assert body["org_id"] == two_orgs.tenant_b.org_id
    # 目标租户看得见；超管不带头（= 平台组织）看不到 —— 证明它既没落进平台组织也没落进 A
    assert body["id"] in _product_ids((await client.get(PRODUCTS_URL, headers=headers)).json())
    own = await client.get(PRODUCTS_URL, headers=_headers(two_orgs.super_user_id, two_orgs.super_org_id))
    assert body["id"] not in _product_ids(own.json())


async def test_superuser_invalid_target_is_rejected(client: AsyncClient, two_orgs: TwoOrgs) -> None:
    """超管指定「格式非法 / 不存在 / 已停用」的目标组织 → 400（而不是悄悄退回归属组织）。"""
    base = _headers(two_orgs.super_user_id, two_orgs.super_org_id)
    for bad in ("not-a-uuid", str(uuid.uuid4()), two_orgs.suspended_org_id):
        resp = await client.get(PRODUCTS_URL, headers={**base, SUPERUSER_ORG_HEADER: bad})
        assert resp.status_code == 400, f"{bad} -> {resp.status_code}: {resp.text}"
