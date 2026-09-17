"""SSE 用例：票据鉴权 / ready 短路 / 回放与终态关流 / Last-Event-ID 续传 / 空闲关流。

为什么这些断言重要:
    SSE 是**前端打字机与阶段提示的唯一数据源**，而它有三个容易写错、写错后表现为
    「页面莫名卡住」的点：
      ① 终态判据 —— 少了某个终态事件，前端会一直转圈（重新加载才能恢复）；
      ② 续传游标 —— ``XRange`` 用错边界（含/不含）会重复或漏发事件，打字机出现重字/跳字；
      ③ 状态门 —— 已结束的任务若仍开流尾随，前端会误判「生成中」而永久重连。

事件发布口径与 ai-engine 一致：``data`` 字段是 JSON 字符串且首键为 ``schema_version``
（本文件用 backend 自己的 ``encode_payload`` 构造，等价于 ai-engine ``RedisStreams.xadd`` 的落盘格式）。
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.event_envelope import encode_payload
from tests.conftest import SeededOrg, seed_job

TICKET_URL = "/api/v1/products/{product_id}/stream-ticket"
STREAM_URL = "/api/v1/products/{product_id}/stream"


@pytest.fixture
def short_idle(monkeypatch) -> None:
    """把 SSE 空闲关流阈值压到 1 秒（必须在 settings 构造前生效）。"""
    monkeypatch.setenv("BACKEND_STREAM_IDLE_SECONDS", "1")


def publish_evt(settings: Settings, thread_id: str, payload: dict) -> str:
    """向 ``evt:{thread_id}`` 追加一条事件（模拟 ai-engine 发布）。"""
    client = new_sync_redis(settings)
    try:
        return str(client.xadd(RedisKeys(settings.env).evt(thread_id), encode_payload(payload)))
    finally:
        client.close()


def parse_sse(text: str) -> tuple[list[dict], list[str]]:
    """解析 SSE 响应文本 → ``(事件列表, 注释帧列表)``。

    事件形如 ``{"id": "1-1", "type": "content.chunk", "data": {...}}``；
    注释帧（以 ``:`` 开头）单独归集，它们不触发前端 ``onmessage``。
    """
    events: list[dict] = []
    comments: list[str] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.strip().splitlines() if line]
        if not lines:
            continue
        if lines[0].startswith(":"):
            comments.extend(lines)
            continue
        event: dict = {}
        for line in lines:
            if line.startswith("id: "):
                event["id"] = line[4:]
            elif line.startswith("data: "):
                body = json.loads(line[6:])
                event["type"] = body.get("type")
                event["data"] = body.get("data")
        if event:
            events.append(event)
    return events, comments


async def test_stream_ticket_requires_read_role_and_hides_other_tenant(
    client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg
):
    """票据接口：未登录 401；跨租户/不存在 → 404。"""
    assert (await client.get(TICKET_URL.format(product_id=seeded_org.product_id))).status_code == 401
    assert (
        await client.get(TICKET_URL.format(product_id=str(uuid.uuid4())), headers=auth_headers)
    ).status_code == 404


async def test_stream_ticket_is_bound_to_product_and_kind(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg, settings: Settings):
    """票据绑定商品与 kind=sse，TTL 与配置一致（长连接与短时效 access 解耦）。"""
    from pa_backend.core import security

    resp = await client.get(TICKET_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["expires_in"] == settings.stream_ticket_ttl_seconds
    claims = security.decode_token(data["ticket"], settings)
    assert claims["kind"] == security.KIND_SSE
    assert claims["product_id"] == seeded_org.product_id
    assert claims["org_id"] == seeded_org.org_id


async def test_ready_shortcut_when_no_active_task(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """无进行中任务（draft 且无线程）：只发一条 ``ready`` 控制帧并关流（前端不必重连）。"""
    resp = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"  # nginx 关缓冲（否则打字机会被攒成一坨）
    events, comments = parse_sse(resp.text)
    assert len(events) == 1
    assert events[0]["type"] == "ready"
    assert events[0]["data"]["status"] == "draft"
    assert comments == []  # 短路路径不产生空闲注释帧


async def test_replay_and_terminal_closes_stream(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """回放历史并尾随：收到终态 ``done`` 立即关流（否则前端会一直转圈）。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    publish_evt(settings, thread_id, {"type": "generate.started"})
    publish_evt(settings, thread_id, {"type": "stage.generating", "data": {"attempt": 1}})
    publish_evt(settings, thread_id, {"type": "content.chunk", "data": {"text": "你好"}})
    publish_evt(settings, thread_id, {"type": "content.chunk", "data": {"text": "，世界"}})
    publish_evt(settings, thread_id, {"type": "done", "data": {"product_id": seeded_org.product_id}})

    resp = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    events, _comments = parse_sse(resp.text)
    assert [event["type"] for event in events] == [
        "generate.started",
        "stage.generating",
        "content.chunk",
        "content.chunk",
        "done",
    ]
    assert [event["data"]["text"] for event in events if event["type"] == "content.chunk"] == ["你好", "，世界"]
    assert all(event["id"].count("-") == 1 for event in events)  # 每条都带 Stream 消息 ID
    assert "schema_version" not in json.dumps(events)  # 过程流只透传 type/data


async def test_hitl_waiting_also_closes_stream(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """``hitl.waiting`` 也关流（PA 特有）：等待人工审批可能数小时，长连接无意义。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    publish_evt(settings, thread_id, {"type": "hitl.waiting", "data": {"product_id": seeded_org.product_id}})
    resp = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    events, _comments = parse_sse(resp.text)
    assert [event["type"] for event in events] == ["hitl.waiting"]


async def test_tail_delivers_events_published_after_connection(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """尾随阶段：连接建立后新发布的事件也必须被推送（「实时」而非只回放）。"""
    import asyncio

    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    publish_evt(settings, thread_id, {"type": "generate.started"})

    async def _publish_later() -> None:
        """连接开始尾随后再发布 chunk + done（模拟 worker 持续产出）。"""
        await asyncio.sleep(0.4)
        publish_evt(settings, thread_id, {"type": "content.chunk", "data": {"text": "尾随正文"}})
        publish_evt(settings, thread_id, {"type": "done", "data": {"product_id": seeded_org.product_id}})

    task = asyncio.create_task(_publish_later())
    resp = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    await task
    events, _comments = parse_sse(resp.text)
    assert [event["type"] for event in events] == ["generate.started", "content.chunk", "done"]


async def test_last_event_id_resume_does_not_repeat(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """续传：带 ``Last-Event-ID`` 时从该条**之后**开始（独占下界），不重发已收到的那条。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    publish_evt(settings, thread_id, {"type": "generate.started"})
    second_id = publish_evt(settings, thread_id, {"type": "stage.generating", "data": {"attempt": 1}})
    publish_evt(settings, thread_id, {"type": "done", "data": {"product_id": seeded_org.product_id}})

    resp = await client.get(
        STREAM_URL.format(product_id=seeded_org.product_id),
        headers={**auth_headers, "Last-Event-ID": second_id},
    )
    events, _comments = parse_sse(resp.text)
    assert [event["type"] for event in events] == ["done"]  # 前两条都不再重发


async def test_idle_close_emits_comment_frame(
    short_idle, client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """空闲关流：无新事件时发注释帧（不产生事件），前端据此带游标续连。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    publish_evt(settings, thread_id, {"type": "generate.started"})
    resp = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    events, comments = parse_sse(resp.text)
    assert [event["type"] for event in events] == ["generate.started"]
    assert any("stream-idle-close" in line for line in comments)


async def test_ticket_path_auth_and_product_binding(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """票据路径：无凭证 401；票据绑定别的商品 → 404；本商品票据 → 正常回放。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    publish_evt(settings, thread_id, {"type": "done", "data": {"product_id": seeded_org.product_id}})

    assert (await client.get(STREAM_URL.format(product_id=seeded_org.product_id))).status_code == 401

    ticket_resp = await client.get(TICKET_URL.format(product_id=seeded_org.product_id), headers=auth_headers)
    ticket = ticket_resp.json()["data"]["ticket"]

    other = await client.post(
        "/api/v1/products", json={"sku_code": "SKU-SSE-OTHER", "title": "别的商品"}, headers=auth_headers
    )
    other_id = other.json()["data"]["id"]
    mismatched = await client.get(STREAM_URL.format(product_id=other_id), params={"ticket": ticket})
    assert mismatched.status_code == 404  # 票据只对签发的那个商品有效

    matched = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), params={"ticket": ticket})
    assert matched.status_code == 200
    events, _comments = parse_sse(matched.text)
    assert [event["type"] for event in events] == ["done"]


async def test_garbage_ticket_is_rejected(client: AsyncClient, auth_headers: dict, seeded_org: SeededOrg):
    """伪票据 → 401（不能靠 SSE 端点绕过鉴权）。"""
    bad = await client.get(STREAM_URL.format(product_id=seeded_org.product_id), params={"ticket": "garbage"})
    assert bad.status_code == 401
