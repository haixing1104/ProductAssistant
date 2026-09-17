"""成员管理用例（RBAC + 软禁用 + 分页）。

覆盖:
    · 只有 admin 能进（reviewer / operator 一律 403）；
    · 本接口只能创建 reviewer / operator（``admin`` 只能在注册流程产生）；
    · ``(org_id, username)`` 唯一冲突 → 409；跨租户不可见/不可操作 → 404；
    · 停用为**软禁用**：``status='disabled'``，此后该账号立即无法登录。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import SeededOrg

USERS_URL = "/api/v1/users"
LOGIN_URL = "/api/v1/auth/login"


async def _create_member(client: AsyncClient, admin_headers: dict, role: str) -> dict:
    """通过接口新增成员并返回响应体（用户名随机，避免跨用例冲突）。"""
    username = f"member_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        USERS_URL,
        json={"username": username, "password": "Pa-Member-Passw0rd!", "role": role},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _login(client: AsyncClient, username: str, password: str) -> str:
    """登录并返回 access token。"""
    resp = await client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def test_admin_can_list_members_with_total_header(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg
):
    """列表返回数组 + ``X-Total-Count``（前端分页读头部）。"""
    resp = await client.get(USERS_URL, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert int(resp.headers["X-Total-Count"]) >= 1
    usernames = [item["username"] for item in resp.json()["data"]]
    assert seeded_org.username in usernames
    for item in resp.json()["data"]:
        assert "hashed_password" not in item  # 绝不外泄哈希


async def test_only_admin_can_access_member_endpoints(client: AsyncClient, auth_headers: dict):
    """reviewer / operator 访问成员接口一律 403（RBAC 红线）。"""
    for role in ("reviewer", "operator"):
        created = await _create_member(client, auth_headers, role)
        token = await _login(client, created["username"], "Pa-Member-Passw0rd!")
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get(USERS_URL, headers=headers)).status_code == 403
        assert (
            await client.post(
                USERS_URL,
                json={"username": f"x_{uuid.uuid4().hex[:6]}", "password": "Pa-Member-Passw0rd!", "role": "operator"},
                headers=headers,
            )
        ).status_code == 403
        assert (await client.post(f"{USERS_URL}/{created['id']}/disable", headers=headers)).status_code == 403


async def test_duplicate_username_returns_409(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """同组织内用户名重复 → 409（唯一约束 ``uq_sys_users_org_username``）。"""
    resp = await client.post(
        USERS_URL,
        json={"username": seeded_org.username, "password": "Pa-Member-Passw0rd!", "role": "operator"},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 409


async def test_creating_admin_role_is_rejected(client: AsyncClient, auth_headers: dict):
    """不允许经本接口创建 admin（防提权）→ 参数校验 400。"""
    resp = await client.post(
        USERS_URL,
        json={"username": "sneaky_admin", "password": "Pa-Member-Passw0rd!", "role": "admin"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_disable_member_blocks_login_and_is_idempotent(client: AsyncClient, auth_headers: dict):
    """停用后立即无法登录；重复停用幂等（都返回 disabled 状态）。"""
    created = await _create_member(client, auth_headers, "operator")
    assert await _login(client, created["username"], "Pa-Member-Passw0rd!")

    first = await client.post(f"{USERS_URL}/{created['id']}/disable", headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "disabled"

    second = await client.post(f"{USERS_URL}/{created['id']}/disable", headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "disabled"

    denied = await client.post(
        LOGIN_URL, json={"username": created["username"], "password": "Pa-Member-Passw0rd!"}
    )
    assert denied.status_code == 401


async def test_disable_admin_is_rejected(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """管理员账号不可停用（这一条同时保护「不能把自己关在门外」）。"""
    resp = await client.post(f"{USERS_URL}/{seeded_org.user_id}/disable", headers=auth_headers)
    assert resp.status_code == 409
    assert "管理员" in resp.json()["message"]


async def test_disable_unknown_or_bad_id_returns_404(client: AsyncClient, auth_headers: dict):
    """未知 id 与非法 id 都返回 404（与越权同响应，不给探测信号）。"""
    unknown = await client.post(f"{USERS_URL}/{uuid.uuid4()}/disable", headers=auth_headers)
    assert unknown.status_code == 404
    bad_format = await client.post(f"{USERS_URL}/not-a-uuid/disable", headers=auth_headers)
    assert bad_format.status_code == 404


async def test_tenant_isolation_hides_other_org_members(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str
):
    """跨租户不可见也不可操作（查询层强制带 ``org_id``）。"""
    from tests.test_auth import _drop_org, _seed_same_username_org

    other_org = None
    try:
        other_org, other_user_id = _seed_same_username_org(
            backend_dsn, f"outsider_{uuid.uuid4().hex[:6]}", "Pa-Other-Passw0rd!"
        )
        listing = await client.get(USERS_URL, headers=auth_headers)
        assert other_user_id not in {item["id"] for item in listing.json()["data"]}
        cross = await client.post(f"{USERS_URL}/{other_user_id}/disable", headers=auth_headers)
        assert cross.status_code == 404
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)
