"""合规词库 / 规则 CRUD 用例（P6）。

要点（都是「错了会静默影响所有组织」的点）:
    · 这两张表是**全局配置**（无 org_id）：写操作必须只对 admin 开放（operator/reviewer → 403）；
    · **重复检测**：表上没有唯一约束，重复词/重复正则会让同一片段被重复上报；
    · **正则语法入口校验**：坏正则在 ai-engine 侧是静默跳过的（规则看起来生效、实际没拦）；
    · **时效窗口**：``expires_at <= effective_at`` 属配置错误，必须当场拒绝（否则一入库就过期）。

清理策略：用例只创建带唯一后缀（``pytest-<uuid>``）的词/规则，并在 finally 里按 id 删除，
绝不动 ``0003_seed.sql`` 的种子词（其它用例依赖它们）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import _exec

WORDS_URL = "/api/v1/compliance/words"
RULES_URL = "/api/v1/compliance/rules"


def _unique(prefix: str = "pytest") -> str:
    """生成唯一词面/规则串（避免与种子词或其它用例冲突）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _delete_word(backend_dsn: str, word_id: str) -> None:
    """按 id 清理词条（role_pa_backend 对 compliance_* 有全 DML 权限）。"""
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.compliance_words WHERE id = %s", (word_id,))


def _delete_rule(backend_dsn: str, rule_id: str) -> None:
    """按 id 清理正则规则。"""
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.compliance_rules WHERE id = %s", (rule_id,))


async def test_list_words_returns_seeded_dictionary(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """词表列表：能看到种子词（0003_seed.sql），并带生效状态与总分页头。"""
    resp = await client.get(WORDS_URL, params={"limit": 200}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert int(resp.headers["X-Total-Count"]) >= 3
    words = {item["word"]: item for item in resp.json()["data"]}
    assert {"国家级", "最便宜", "疗效"} <= set(words)
    assert words["国家级"]["severity"] == "high"
    assert words["国家级"]["is_active"] is True


async def test_list_words_filters(client: AsyncClient, auth_headers: dict):
    """过滤：按严重级 / 仅生效中 / 词面模糊。"""
    by_severity = await client.get(WORDS_URL, params={"severity": "high", "limit": 200}, headers=auth_headers)
    assert all(item["severity"] == "high" for item in by_severity.json()["data"])

    by_keyword = await client.get(WORDS_URL, params={"keyword": "国家", "limit": 200}, headers=auth_headers)
    assert any(item["word"] == "国家级" for item in by_keyword.json()["data"])

    active = await client.get(WORDS_URL, params={"active_only": True, "limit": 200}, headers=auth_headers)
    assert all(item["is_active"] for item in active.json()["data"])


async def test_create_word_with_future_effective_window(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """新增词条：未来生效的词 ``is_active=False``（前端据此标注「未生效」）。"""
    word = _unique()
    created_id = None
    try:
        resp = await client.post(
            WORDS_URL,
            json={"word": word, "severity": "medium", "source": "pytest", "effective_at": "2099-01-01T00:00:00Z"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        created_id = data["id"]
        assert data["severity"] == "medium"
        assert data["is_active"] is False  # 未来生效
    finally:
        if created_id:
            _delete_word(backend_dsn, created_id)


async def test_create_word_rejects_duplicate_blank_and_bad_window(client: AsyncClient, auth_headers: dict):
    """入口校验：重复词 409；空白词 400；``expires_at <= effective_at`` 400；非法严重级 400。"""
    dup = await client.post(WORDS_URL, json={"word": "国家级", "severity": "high"}, headers=auth_headers)
    assert dup.status_code == 409

    blank = await client.post(WORDS_URL, json={"word": "   ", "severity": "high"}, headers=auth_headers)
    assert blank.status_code == 400

    bad_window = await client.post(
        WORDS_URL,
        json={
            "word": _unique(),
            "severity": "high",
            "effective_at": "2099-01-01T00:00:00Z",
            "expires_at": "2098-01-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert bad_window.status_code == 400

    bad_severity = await client.post(WORDS_URL, json={"word": _unique(), "severity": "urgent"}, headers=auth_headers)
    assert bad_severity.status_code == 400


async def test_update_and_delete_word(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """更新（改名 + 改严重级）与删除；重复名 409；未知 id 404。"""
    word = _unique()
    created = (
        await client.post(WORDS_URL, json={"word": word, "severity": "low"}, headers=auth_headers)
    ).json()["data"]
    try:
        renamed = await client.patch(
            f"{WORDS_URL}/{created['id']}",
            json={"word": f"{word}-改", "severity": "high", "source": "改造"},
            headers=auth_headers,
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["data"] == {
            **renamed.json()["data"],
            "word": f"{word}-改",
            "severity": "high",
            "source": "改造",
        }

        conflict = await client.patch(
            f"{WORDS_URL}/{created['id']}", json={"word": "国家级"}, headers=auth_headers
        )
        assert conflict.status_code == 409

        deleted = await client.delete(f"{WORDS_URL}/{created['id']}", headers=auth_headers)
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True

        again = await client.delete(f"{WORDS_URL}/{created['id']}", headers=auth_headers)
        assert again.status_code == 404
        missing = await client.patch(f"{WORDS_URL}/{uuid.uuid4()}", json={"severity": "high"}, headers=auth_headers)
        assert missing.status_code == 404
    finally:
        _delete_word(backend_dsn, created["id"])


async def test_rule_crud_validates_regex_syntax(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """正则规则：语法错误 400（坏正则会被 ai-engine 静默跳过）；重复 409；增删改闭环。"""
    pattern = f"pytest-{uuid.uuid4().hex[:8]}"
    created_id = None
    try:
        bad = await client.post(
            RULES_URL, json={"pattern": "(未闭合", "severity": "high"}, headers=auth_headers
        )
        assert bad.status_code == 400
        assert "正则" in bad.json()["message"]

        created = await client.post(
            RULES_URL,
            json={"pattern": pattern, "severity": "high", "suggestion": "请改写"},
            headers=auth_headers,
        )
        assert created.status_code == 200, created.text
        created_id = created.json()["data"]["id"]
        assert created.json()["data"]["type"] == "regex"

        dup = await client.post(RULES_URL, json={"pattern": pattern, "severity": "high"}, headers=auth_headers)
        assert dup.status_code == 409

        patched = await client.patch(
            f"{RULES_URL}/{created_id}", json={"severity": "medium", "suggestion": None}, headers=auth_headers
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["severity"] == "medium"
        assert patched.json()["data"]["suggestion"] is None

        listed = await client.get(RULES_URL, params={"severity": "medium", "limit": 200}, headers=auth_headers)
        assert created_id in {item["id"] for item in listed.json()["data"]}

        deleted = await client.delete(f"{RULES_URL}/{created_id}", headers=auth_headers)
        assert deleted.status_code == 200
        assert (await client.delete(f"{RULES_URL}/{created_id}", headers=auth_headers)).status_code == 404
    finally:
        if created_id:
            _delete_rule(backend_dsn, created_id)


async def test_only_admin_can_write_and_read_is_limited_to_admin_and_reviewer(
    client: AsyncClient, auth_headers: dict, backend_dsn: str
):
    """RBAC：写仅 admin；读允许 admin/reviewer，operator 连读都不行（避免误以为是普通业务数据）。

    全局配置的越权写会让**所有组织**的生成结果变化，因此这里断言的是「拒绝」而不是「允许」。
    """
    from tests.test_members_rbac import _create_member, _login

    operator = await _create_member(client, auth_headers, "operator")
    reviewer = await _create_member(client, auth_headers, "reviewer")
    op_headers = {"Authorization": f"Bearer {await _login(client, operator['username'], 'Pa-Member-Passw0rd!')}"}
    rv_headers = {"Authorization": f"Bearer {await _login(client, reviewer['username'], 'Pa-Member-Passw0rd!')}"}

    assert (await client.get(WORDS_URL, headers=op_headers)).status_code == 403
    assert (await client.post(WORDS_URL, json={"word": _unique()}, headers=op_headers)).status_code == 403

    assert (await client.get(WORDS_URL, headers=rv_headers)).status_code == 200  # 审批人可读
    assert (await client.get(RULES_URL, headers=rv_headers)).status_code == 200
    write_as_reviewer = await client.post(RULES_URL, json={"pattern": _unique()}, headers=rv_headers)
    assert write_as_reviewer.status_code == 403

    # 未被越权写污染：词表里没有 operator 那次尝试的词
    listed = await client.get(WORDS_URL, params={"limit": 200}, headers=auth_headers)
    assert all(not item["word"].startswith("pytest") for item in listed.json()["data"])
