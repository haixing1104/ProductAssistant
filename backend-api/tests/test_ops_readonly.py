"""运维只读面用例（P6）：心跳存活判定 / 流与消费组 / DLQ 回看。

为什么这些断言重要:
    · **「一个心跳都没有」必须被判为 stalled**：若把它当作「一切正常」，面板会在消费侧彻底停滞时
      显示绿色 —— 这是运维面最危险的一种错；
    · **单指标缺失不能让整页报错**：某个流还没被消费过（没建组）属正常，接口必须仍然 200；
    · **只读**：本模块不写任何键；用例用「接口调用前后 Redis 内容不变」来钉住这一点。

所有用例自带清理（心跳键 / DLQ 消息用完即删），不污染本地 dev 环境。
"""

from __future__ import annotations

import json
import uuid

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis

OVERVIEW_URL = "/api/v1/ops/overview"
DLQ_URL = "/api/v1/ops/dlq"


def _redis(settings: Settings):
    """同步 Redis 客户端（用例自建自关）。"""
    return new_sync_redis(settings)


async def test_overview_reports_heartbeat_alive_then_stalled(client: AsyncClient, auth_headers: dict, settings: Settings):
    """心跳键在 → ``alive``；键删除（模拟进程退出）→ ``stalled=True``。"""
    consumer = f"pytest-worker-{uuid.uuid4().hex[:6]}"
    key = RedisKeys(settings.env).heartbeat(consumer)
    client_redis = _redis(settings)
    try:
        client_redis.set(key, "1", ex=60)
        resp = await client.get(OVERVIEW_URL, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        worker = next(item for item in data["workers"] if item["consumer"] == consumer)
        assert worker["alive"] is True and worker["ttl_seconds"] > 0
        assert data["stalled"] is False

        client_redis.delete(key)
        after = (await client.get(OVERVIEW_URL, headers=auth_headers)).json()["data"]
        assert all(item["consumer"] != consumer for item in after["workers"])
        # 没有其它 worker 在跑 → 明确判为「消费停滞」
        assert after["stalled"] is True
    finally:
        client_redis.delete(key)
        client_redis.close()


async def test_overview_lists_contract_streams_even_when_empty(
    client: AsyncClient, auth_headers: dict, settings: Settings
):
    """契约流逐条返回（长度 + 消费组）；未建组不报错（属正常状态而非故障）。"""
    resp = await client.get(OVERVIEW_URL, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    streams = {item["name"]: item for item in resp.json()["data"]["streams"]}
    assert set(streams) == {"job_generate", "job_approval", "job_product_purge", "workflow_result"}
    assert streams["job_generate"]["key"] == RedisKeys(settings.env).job_generate()
    assert isinstance(streams["job_generate"]["length"], int)
    assert isinstance(streams["job_generate"]["groups"], list)
    assert "error" not in streams["job_generate"]


async def test_dlq_entries_are_readable_and_readonly(
    client: AsyncClient, auth_headers: dict, settings: Settings
):
    """DLQ 回看：能读到消息载荷；接口调用**不改变** Redis 内容（只读红线）。"""
    domain = "job:generate"
    key = RedisKeys(settings.env).dlq(domain)
    thread_id = str(uuid.uuid4())
    client_redis = _redis(settings)
    try:
        client_redis.xadd(key, {"data": json.dumps({"schema_version": 1, "thread_id": thread_id})})
        client_redis.xadd(key, {"data": "not-json"})  # 脏消息在 DLQ 里属正常，必须不抛异常

        resp = await client.get(DLQ_URL, params={"domain": domain, "limit": 10}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["key"] == key
        assert data["length"] == int(client_redis.xlen(key))
        payloads = [entry["payload"] for entry in data["entries"]]
        assert {"schema_version": 1, "thread_id": thread_id} in payloads
        # 脏消息：payload 为 None 且给出 decode_error（而不是整页 500）
        assert any(entry.get("decode_error") for entry in data["entries"] if entry["payload"] is None)

        overview = (await client.get(OVERVIEW_URL, headers=auth_headers)).json()["data"]
        assert any(item["domain"] == domain for item in overview["dlq"])

        # 只读：两次调用后消息数不变
        await client.get(DLQ_URL, params={"domain": domain}, headers=auth_headers)
        assert int(client_redis.xlen(key)) == data["length"]
    finally:
        client_redis.delete(key)
        client_redis.close()


async def test_dlq_unknown_domain_returns_empty_not_error(client: AsyncClient, auth_headers: dict):
    """未知死信域：返回 0 条而不是 404/500（面板轮询不能因为某个域为空就报错）。"""
    resp = await client.get(DLQ_URL, params={"domain": "job:no-such"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["length"] == 0
    assert resp.json()["data"]["entries"] == []


async def test_ops_endpoints_require_admin(client: AsyncClient, auth_headers: dict):
    """运维面仅 admin：operator / reviewer 一律 403（键名与消息内容属内部信息）。"""
    from tests.test_members_rbac import _create_member, _login

    for role in ("operator", "reviewer"):
        member = await _create_member(client, auth_headers, role)
        headers = {"Authorization": f"Bearer {await _login(client, member['username'], 'Pa-Member-Passw0rd!')}"}
        assert (await client.get(OVERVIEW_URL, headers=headers)).status_code == 403
        assert (await client.get(DLQ_URL, params={"domain": "job:generate"}, headers=headers)).status_code == 403