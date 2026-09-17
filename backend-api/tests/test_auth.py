"""认证用例：注册 / 登录 / 续签轮换 / 登出 / 身份。

覆盖的**PA 专属**点（相对 ProductPilot，见``services/auth_service.py`` 模块 docstring）:
    · 同名账号跨组织消歧：``sys_users`` 唯一键是 ``(org_id, username)``，
      候选 → 校验口令 → 唯一命中即登录成功；同名同口令才要求提供 ``org_name``。
    · 账号停用（``status='disabled'``）后立即无法登录。

安全断言（都是「不能退化成更宽松」的红线）:
    · 用户不存在与口令错误返回**同一句话**（防账号枚举）；
    · refresh 必须带 CSRF 头；旧 refresh 二次使用 → 整户会话吊销。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from pa_backend.core.security import hash_password
from pa_backend.services.auth_service import INVALID_CREDENTIALS_DETAIL
from tests.conftest import SeededOrg, _exec

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
CSRF = {"X-Requested-With": "fetch"}

#: 与 infra/.env.template 的 BACKEND_REFRESH_COOKIE_NAME 默认值一致
REFRESH_COOKIE = "pa_refresh"


def _seed_same_username_org(backend_dsn: str, username: str, password: str) -> tuple[str, str]:
    """另建一个组织 + 同名用户（模拟「跨组织同名账号」）。

    参数:
        backend_dsn: role_pa_backend DSN（业务域表只有 backend 能写）。
        username: 与已有账号相同的用户名。
        password: 明文口令（与另一个账号相同即构造「无法消歧」场景）。
    返回:
        ``(org_id, user_id)``。
    """
    org_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, %s, 'active')",
        (org_id, f"第二组织-{org_id[:8]}"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users "
        "(id, org_id, username, hashed_password, role, status) VALUES (%s, %s, %s, %s, 'admin', 'active')",
        (user_id, org_id, username, hash_password(password)),
    )
    return org_id, user_id


def _drop_org(backend_dsn: str, org_id: str) -> None:
    """删除测试中临时建的组织及其数据（先子表后父表，避免外键约束报错）。

    顺序即依赖：业务子表 → ``products`` → ``sys_users`` → ``organizations``。
    **刻意不删 ``delete_audits``**：它只能追加（``0004`` REVOKE UPDATE/DELETE），
    这里的 DELETE 会被数据库直接拒绝 —— 那正是权限红线的证据（见 ``test_roles_red_lines``）。
    """
    for table in ("notification_outbox", "hitl_approvals", "generation_jobs"):
        _exec(backend_dsn, f"DELETE FROM schema_pa_backend.{table} WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.sys_users WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.organizations WHERE id = %s", (org_id,))


async def test_register_creates_tenant_with_admin_role(client: AsyncClient):
    """注册即开租户：返回 user_id/org_id，且该用户是 admin（首个管理员）。"""
    username = f"owner_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        REGISTER_URL, json={"org_name": "注册回归组织", "username": username, "password": "Pa-Register-123"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["role"] == "admin"
    assert data["org_id"] and data["user_id"]


async def test_register_rejects_short_password(client: AsyncClient):
    """口令长度不足必须在入口被拒（400），而不是进库后才报错。"""
    resp = await client.post(
        REGISTER_URL, json={"org_name": "组织", "username": "shortpw", "password": "123"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 400


async def test_login_success_returns_access_and_http_only_refresh_cookie(
    client: AsyncClient, seeded_org: SeededOrg
):
    """登录成功：响应体只有 access（refresh 走 HttpOnly Cookie）。"""
    resp = await client.post(
        LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["access_token"] and body["token_type"] == "bearer"
    assert "refresh" not in body  # refresh 绝不出现在响应体里
    cookie_header = resp.headers.get("set-cookie", "")
    assert REFRESH_COOKIE in cookie_header and "HttpOnly" in cookie_header
    assert client.cookies.get(REFRESH_COOKIE)

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["data"]["username"] == seeded_org.username
    assert me.json()["data"]["role"] == "admin"


async def test_login_failure_message_does_not_leak_account_existence(
    client: AsyncClient, seeded_org: SeededOrg
):
    """「用户不存在」与「口令错误」必须返回同一句话（防账号枚举）。"""
    unknown = await client.post(LOGIN_URL, json={"username": "no-such-user", "password": "whatever-123"})
    wrong = await client.post(
        LOGIN_URL, json={"username": seeded_org.username, "password": "definitely-wrong"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["message"] == wrong.json()["message"] == INVALID_CREDENTIALS_DETAIL


async def test_login_rejected_for_disabled_user(client: AsyncClient, seeded_org: SeededOrg, backend_dsn: str):
    """账号被停用后立即无法登录（JWT 未过期也不行 —— 登录路径每次都查库）。"""
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.sys_users SET status = 'disabled' WHERE id = %s",
        (seeded_org.user_id,),
    )
    try:
        resp = await client.post(
            LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password}
        )
        assert resp.status_code == 401
    finally:
        _exec(
            backend_dsn,
            "UPDATE schema_pa_backend.sys_users SET status = 'active' WHERE id = %s",
            (seeded_org.user_id,),
        )


async def test_login_disambiguates_same_username_across_orgs_by_password(
    client: AsyncClient, seeded_org: SeededOrg, backend_dsn: str
):
    """跨组织同名账号：口令不同 → 无需 ``org_name`` 也能唯一确定身份（正常用户零感知）。"""
    other_org = None
    try:
        other_org, _ = _seed_same_username_org(
            backend_dsn, seeded_org.username, "Another-Org-Passw0rd!"
        )
        resp = await client.post(
            LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["data"]["access_token"]
        me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["data"]["org_id"] == seeded_org.org_id  # 登的是「口令匹配」的那个组织
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)


async def test_login_requires_org_name_when_username_and_password_both_collide(
    client: AsyncClient, seeded_org: SeededOrg, backend_dsn: str
):
    """同名**且**同口令：无法唯一确定身份 → 400 并要求提供 ``org_name``（而不是随便登一个）。"""
    other_org = None
    try:
        other_org, _ = _seed_same_username_org(backend_dsn, seeded_org.username, seeded_org.password)
        ambiguous = await client.post(
            LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password}
        )
        assert ambiguous.status_code == 400
        assert "org_name" in ambiguous.json()["message"]
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)


async def test_login_with_org_name_resolves_collision(
    client: AsyncClient, seeded_org: SeededOrg, backend_dsn: str
):
    """提供 ``org_name`` 后即使同名同口令也能确定身份；组织名写错则与「账号不存在」同一响应。"""
    other_org = None
    try:
        other_org, _ = _seed_same_username_org(backend_dsn, seeded_org.username, seeded_org.password)
        ok_resp = await client.post(
            LOGIN_URL,
            json={
                "username": seeded_org.username,
                "password": seeded_org.password,
                "org_name": f"测试组织-{seeded_org.org_id[:8]}",
            },
        )
        assert ok_resp.status_code == 200, ok_resp.text
        wrong_org = await client.post(
            LOGIN_URL,
            json={
                "username": seeded_org.username,
                "password": seeded_org.password,
                "org_name": "不存在的组织",
            },
        )
        assert wrong_org.status_code == 401
    finally:
        if other_org:
            _drop_org(backend_dsn, other_org)


async def test_refresh_requires_csrf_header(client: AsyncClient, seeded_org: SeededOrg):
    """refresh 缺少 ``X-Requested-With: fetch`` → 403（CSRF 双保险）。"""
    await client.post(LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password})
    resp = await client.post(REFRESH_URL)
    assert resp.status_code == 403


async def test_refresh_rotates_and_detects_reuse(client: AsyncClient, seeded_org: SeededOrg):
    """轮换：旧 refresh 作废；旧 refresh 二次使用视为被盗 → 整户会话吊销。"""
    await client.post(LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password})
    first = client.cookies.get(REFRESH_COOKIE)
    assert first

    rotated = await client.post(REFRESH_URL, headers=CSRF)
    assert rotated.status_code == 200, rotated.text
    second = client.cookies.get(REFRESH_COOKIE)
    assert second and second != first  # 必须轮换

    # 重放旧 token（模拟被盗使用）
    client.cookies.set(REFRESH_COOKIE, first, path="/api/v1/auth")
    reuse = await client.post(REFRESH_URL, headers=CSRF)
    assert reuse.status_code == 401
    assert "吊销" in reuse.json()["message"]

    # 整户吊销：新 token 也一并失效（攻击者拿到的与被盗用户手上的都作废）
    client.cookies.set(REFRESH_COOKIE, second, path="/api/v1/auth")
    after = await client.post(REFRESH_URL, headers=CSRF)
    assert after.status_code == 401


async def test_logout_revokes_refresh_session(client: AsyncClient, seeded_org: SeededOrg):
    """登出后原 refresh 不能再换 access。"""
    await client.post(LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password})
    token = client.cookies.get(REFRESH_COOKIE)
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    client.cookies.set(REFRESH_COOKIE, token, path="/api/v1/auth")
    resp = await client.post(REFRESH_URL, headers=CSRF)
    assert resp.status_code == 401


async def test_me_requires_valid_token(client: AsyncClient):
    """``/auth/me``：无令牌与坏令牌都是 401（统一信封）。"""
    anon = await client.get("/api/v1/auth/me")
    assert anon.status_code == 401
    bad = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401
    assert set(bad.json()) == {"code", "data", "message"}

