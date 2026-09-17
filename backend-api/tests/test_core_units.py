"""core 层单测：纯函数与配置（无需 DB/Redis，宿主机就能跑）。

这类用例的价值在于**把契约钉死**：keyspace 拼错、JWT 少校验一个 kind、DSN 派生漏了 URL 编码，
都不会在本地「看起来正常」时报出来，只会在联调或生产暴露。故用断言锁住。
"""

from __future__ import annotations

import uuid as uuid_module

import pytest
from fastapi import HTTPException

from pa_backend.core import security
from pa_backend.core.config import Settings
from pa_backend.core.errors import ApiError, envelope, ok
from pa_backend.core.keys import RedisKeys
from pa_backend.routers.common import to_uuid

# --------------------------------------------------------------- keyspace 契约


def test_keyspace_matches_ai_engine_contract():
    """契约键必须与 ai-engine ``RedisKeys`` 完全同名（改一个字符 = 链路静默断掉）。

    出处：``ai-engine/src/adapters/redis_eventbus.py`` 的 keyspace 键模板。
    """
    keys = RedisKeys("test")
    assert keys.job_generate() == "pa:test:job:generate"
    assert keys.job_approval() == "pa:test:job:approval"
    assert keys.job_product_purge() == "pa:test:job:product_purge"
    assert keys.workflow_result() == "pa:test:result:workflow"
    assert keys.evt("tid-1") == "pa:test:evt:tid-1"
    assert keys.dlq("job:generate") == "pa:test:dlq:job:generate"
    assert keys.heartbeat("worker-1") == "pa:test:worker:heartbeat:worker-1"


def test_keyspace_backend_private_keys_are_not_contract_keys():
    """backend 私有键（会话/限流）必须落在 ``auth:`` 命名空间，不能占用契约前缀。

    原因：``job:`` / ``result:`` / ``evt:`` / ``dlq:`` / ``worker:`` 是两侧共用命名空间，
    私有状态混进去会被 worker 的 PEL/心跳巡检误读。
    """
    keys = RedisKeys("prod")
    for value in (
        keys.auth_refresh_session("tok"),
        keys.auth_refresh_owner("tok"),
        keys.auth_refresh_user_set("uid"),
        keys.auth_login_fail("u:1.1.1.1"),
        keys.auth_login_lock("u:1.1.1.1"),
    ):
        assert value.startswith("pa:prod:auth:")
        assert ":job:" not in value and ":evt:" not in value and ":result:" not in value


def test_keys_do_not_expose_worker_internal_lock_and_done():
    """backend 不得提供 ``lock:`` / ``done:`` 构造器（worker 内部语义，碰了会坏幂等）。"""
    assert not hasattr(RedisKeys("dev"), "lock")
    assert not hasattr(RedisKeys("dev"), "done")


# --------------------------------------------------------------- 安全与凭证


def test_password_hash_roundtrip_and_rejects_wrong_password():
    """bcrypt 哈希可校验，且错误口令必须为 False（哈希脏数据不抛异常）。"""
    hashed = security.hash_password("Pa-Test-Passw0rd!")
    assert hashed != "Pa-Test-Passw0rd!"
    assert security.verify_password("Pa-Test-Passw0rd!", hashed) is True
    assert security.verify_password("wrong", hashed) is False
    assert security.verify_password("x", "not-a-bcrypt-hash") is False


def test_access_token_roundtrip_carries_tenant_and_role():
    """access token 必须携带 ``sub`` / ``org_id`` / ``role``（租户过滤与 RBAC 的来源）。"""
    settings = Settings()
    token = security.create_access_token(user_id="u1", org_id="o1", role="admin", settings=settings)
    claims = security.decode_token(token, settings)
    assert claims["sub"] == "u1"
    assert claims["org_id"] == "o1"
    assert claims["role"] == "admin"


def test_access_token_rejected_with_other_secret(monkeypatch):
    """换密钥即验签失败（防止「本地用默认密钥上线」这类事故被静默放过）。"""
    token = security.create_access_token(user_id="u1", org_id="o1", role="admin", settings=Settings())
    monkeypatch.setenv("JWT_SECRET", "another-secret-value")
    with pytest.raises(Exception):
        security.decode_token(token, Settings())


def test_sse_ticket_binds_product_and_kind():
    """SSE 票据必须绑定 ``product_id`` 并标记 ``kind=sse``（泄露面收敛到单个商品）。"""
    settings = Settings()
    ticket = security.create_sse_ticket(
        user_id="u1", org_id="o1", role="reviewer", product_id="p1", settings=settings
    )
    claims = security.decode_token(ticket, settings)
    assert claims["kind"] == security.KIND_SSE
    assert claims["product_id"] == "p1"


def test_approval_ticket_is_locator_not_authorization():
    """审批深链票据只带定位信息与 kind，**不含任何审批权限字段**。"""
    settings = Settings()
    ticket = security.create_approval_ticket(
        approval_id="a1", org_id="o1", product_id="p1", settings=settings
    )
    claims = security.decode_token(ticket, settings)
    assert claims["kind"] == security.KIND_APPROVAL_REDIRECT
    assert set(claims) >= {"approval_id", "org_id", "product_id", "kind", "exp", "iat"}
    assert "role" not in claims  # 不授予角色 —— 真正审批仍要登录态 + CAS


def test_refresh_token_is_random_and_long_enough():
    """refresh 标识必须随机（两次不同）且有足够熵。"""
    first, second = security.generate_refresh_token(), security.generate_refresh_token()
    assert first != second
    assert len(first) >= 48


# --------------------------------------------------------------- 错误模型与路径工具


def test_response_envelope_shape_is_stable():
    """统一响应形状：``{code, data, message}``（前端只写一份错误处理）。"""
    assert ok({"a": 1}) == {"code": 200, "data": {"a": 1}, "message": "ok"}
    assert envelope(None, 404, "没了") == {"code": 404, "data": None, "message": "没了"}
    error = ApiError(409, "冲突")
    assert error.status_code == 409 and str(error) == "冲突"


def test_to_uuid_rejects_bad_format_with_404():
    """路径 id 非法 → 404（与越权同响应，不给探测信号）。"""
    valid = uuid_module.uuid4()
    assert to_uuid(str(valid)) == valid
    with pytest.raises(HTTPException) as excinfo:
        to_uuid("not-a-uuid")
    assert excinfo.value.status_code == 404
