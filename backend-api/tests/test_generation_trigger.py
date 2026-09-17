"""生成触发用例：状态推进 + **入站载荷契约**（含合规快照时效过滤）。

为什么这些断言必须在**真实 Redis** 上做:
    ``job:generate`` 的载荷是 backend 与 ai-engine 之间唯一的接口。字段名、信封首键
    （``schema_version``）、rules 快照的键名，任何一处写错都不会在本地报错 ——
    只会表现为「worker 收到了消息但规则不生效」或「连消息都解析不了」。所以这里直接读流验证。

时效过滤（契约互补点）:
    ai-engine 的 ``compile_rules_snapshot`` **不做时间过滤**；过期/未生效的规则必须由 backend
    在入队瞬间剔除，否则「已过期词」会长期继续拦截。本文件用两个极端样本（过去已过期、未来未生效）
    把这条互补关系钉住。
"""

from __future__ import annotations

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.ai_engine_client import AIEngineClient
from pa_backend.services.event_envelope import decode_fields
from tests.conftest import SeededOrg, _exec

GENERATE_URL = "/api/v1/products/{product_id}/generate"
PRODUCTS_URL = "/api/v1/products"


def _drain_job_stream(settings: Settings) -> None:
    """清空本环境的 ``job:generate`` 流（让断言只看到本次投递的消息）。"""
    client = new_sync_redis(settings)
    try:
        client.delete(RedisKeys(settings.env).job_generate())
    finally:
        client.close()


def _read_job_messages(settings: Settings) -> list[dict]:
    """读取 ``job:generate`` 全部消息的载荷。"""
    client = new_sync_redis(settings)
    try:
        entries = client.xrange(RedisKeys(settings.env).job_generate(), min="-", max="+", count=200)
        return [decode_fields(fields) for _id, fields in entries]
    finally:
        client.close()


async def test_trigger_generation_advances_state_and_enqueues_snapshot(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, settings: Settings
):
    """触发生成：商品转 generating + job(running) + 流里带 rules 快照（信封首键 schema_version）。"""
    _drain_job_stream(settings)
    resp = await client.post(
        GENERATE_URL.format(product_id=seeded_org.product_id),
        headers={**auth_headers, "X-Request-Id": "req-trace-0001"},
    )
    assert resp.status_code == 200, resp.text
    thread_id = resp.json()["data"]["thread_id"]

    detail = await client.get(f"{PRODUCTS_URL}/{seeded_org.product_id}", headers=auth_headers)
    body = detail.json()["data"]
    assert body["status"] == "generating"
    assert body["active_thread_id"] == thread_id
    assert body["active_job_status"] == "running"

    messages = _read_job_messages(settings)
    assert len(messages) == 1
    payload = messages[0]
    assert payload["schema_version"] == 1  # 信封版本（与 ai-engine EVENT_SCHEMA_VERSION 对齐）
    assert payload["thread_id"] == thread_id
    assert payload["product_id"] == seeded_org.product_id
    assert payload["org_id"] == seeded_org.org_id
    # 请求关联 ID（P8）：把「这次 HTTP 触发」与后续 ai-engine / result 消费日志串起来。
    # 必须等于请求头（前端注入 X-Request-Id）——否则跨进程排查时对不上号。
    assert payload["request_id"] == "req-trace-0001"
    # rules 快照键名必须与 compile_rules_snapshot 读取的键一致
    assert set(payload["rules"]) == {"words", "rules"}
    words = {item["word"] for item in payload["rules"]["words"]}
    assert {"国家级", "最便宜", "疗效"} <= words  # 0003_seed.sql 的种子违禁词
    assert payload["rules"]["rules"], "正则规则应随带（种子里有 2 条）"


async def test_rules_snapshot_filters_by_effective_window(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, settings: Settings, backend_dsn: str
):
    """时效过滤：已过期词与未生效词都不得进入快照（ai-engine 侧不做时间判断）。"""
    expired_id, future_id = "77777777-7777-4777-8777-7777777777aa", "77777777-7777-4777-8777-7777777777bb"
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.compliance_words (id, word, severity, effective_at, expires_at, source) "
        "VALUES (%s, '已过期词', 'high', now() - interval '10 day', now() - interval '1 day', '过期样本')",
        (expired_id,),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.compliance_words (id, word, severity, effective_at, source) "
        "VALUES (%s, '未来生效词', 'high', now() + interval '10 day', '未生效样本')",
        (future_id,),
    )
    try:
        _drain_job_stream(settings)
        resp = await client.post(GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
        assert resp.status_code == 200, resp.text
        words = {item["word"] for item in _read_job_messages(settings)[0]["rules"]["words"]}
        assert "已过期词" not in words
        assert "未来生效词" not in words
        assert "国家级" in words  # 正常生效的词仍在
    finally:
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.compliance_words WHERE id = ANY(%s)", ([expired_id, future_id],))


async def test_second_trigger_while_running_returns_409(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, settings: Settings
):
    """进行中任务未结束前重复触发 → 409（防重复扣费；终态后可以再触发）。"""
    _drain_job_stream(settings)
    first = await client.post(GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    assert first.status_code == 200
    again = await client.post(GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    assert again.status_code == 409
    assert "进行中" in again.json()["message"]


async def test_enqueue_failure_rolls_back_product_state(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, monkeypatch
):
    """投递失败（Redis 不可用）→ 503，且商品状态回滚为 draft（不能永远卡在 generating）。"""

    def _boom(self, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(AIEngineClient, "trigger_generation", _boom)
    resp = await client.post(GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    assert resp.status_code == 503

    detail = await client.get(f"{PRODUCTS_URL}/{seeded_org.product_id}", headers=auth_headers)
    body = detail.json()["data"]
    assert body["status"] == "draft"
    assert body["active_thread_id"] is None


async def test_generate_rejected_for_legacy_deleted_product(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, backend_dsn: str
):
    """历史 ``deleted`` 行不能触发生成（409）。

    软删端点已下线 → 用 SQL 直置该状态（历史数据仍可能存在，守卫必须继续生效）。
    """
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.products SET status = 'deleted' WHERE id = %s",
        (seeded_org.product_id,),
    )
    try:
        resp = await client.post(GENERATE_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
        assert resp.status_code == 409
    finally:
        # 恢复种子商品状态：seeded_org 是**会话级**夹具，别的用例还要用它触发生成
        _exec(
            backend_dsn,
            "UPDATE schema_pa_backend.products SET status = 'draft' WHERE id = %s",
            (seeded_org.product_id,),
        )
