"""通知用例：outbox 落库 / 投递成功 / 重试退避 / DLQ / 渠道解析。

为什么这些断言重要:
    「审批人要收到通知」看起来只是个 HTTP 调用，但真正的风险点是**失败之后怎么办**：
    丢掉 = 审批卡死无人知；重试无上限 = 刷屏 + 打挂钉钉；DLQ 不落 = 无法人工补救。
    本文件把这几条行为钉死。

模式说明:
    ``NOTIFY_MODE=console``（默认）用控制台 sender 把全链路跑通（不发真实请求）；
    ``live`` 模式下「有渠道名但没凭据」应解析为空渠道（不出站），投递器遇到无 sender 的渠道判 DLQ。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from pa_backend.services.notifications.adapters import ConsoleSender, NotificationSendError
from pa_backend.services.notifications.deliverer import OutboxDeliverer
from pa_backend.services.notifications.resolver import build_senders, resolve_channels
from pa_backend.services.workflow_result_consumer import WorkflowResultConsumer
from tests.conftest import SeededOrg, result_payload, seed_job

SNAPSHOT = {
    "reason": "high_value",
    "content": {"blocks": [{"type": "text", "text": "这是一段待审批的文案正文"}]},
    "evaluation_result": {"passed": True, "score": 90.0, "errors": []},
    "evaluation_attempts": 2,
}


def _outbox_rows(dsn: str, org_id: str) -> list[tuple]:
    """读某组织的 outbox 行（status, retry_count, channel, payload, provider_msg_id）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT status, retry_count, channel, payload, provider_msg_id "
            "FROM schema_pa_backend.notification_outbox WHERE org_id = %s ORDER BY created_at",
            (org_id,),
        ).fetchall()


async def test_awaiting_human_enqueues_outbox_with_deep_link(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """转人工时：与待审单**同事务**写入 outbox，载荷含摘要、正文节选与带票据的深链。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    consumer = WorkflowResultConsumer(
        app.state.session_factory, settings=settings, notify_channels=("dingtalk",)
    )
    await consumer.process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=SNAPSHOT,
        )
    )
    rows = _outbox_rows(backend_dsn, seeded_org.org_id)
    assert len(rows) == 1
    status, retry_count, channel, payload, provider_msg_id = rows[0]
    assert (status, retry_count, channel, provider_msg_id) == ("pending", 0, "dingtalk", None)
    assert payload["reason_label"] == "高价商品需人工放行"
    assert payload["sku_code"] == "SKU-" + seeded_org.product_id[:8]
    assert payload["excerpt"] == "这是一段待审批的文案正文"
    assert payload["score"] == 90.0
    assert "ticket=" in payload["deep_link"]  # 深链带一次性签名票据


async def test_awaiting_human_without_channels_skips_outbox(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """未启用任何渠道：仍建待审单，但**不**写 outbox（本地/测试默认行为）。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    await WorkflowResultConsumer(app.state.session_factory, settings=settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=SNAPSHOT,
        )
    )
    assert _outbox_rows(backend_dsn, seeded_org.org_id) == []


async def test_console_sender_marks_sent(app, settings, backend_dsn: str, seeded_org: SeededOrg):
    """控制台投递：``process_due_once`` 把待发行推进为 sent 并记录回执号。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    await WorkflowResultConsumer(
        app.state.session_factory, settings=settings, notify_channels=("dingtalk",)
    ).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=SNAPSHOT,
        )
    )
    deliverer = OutboxDeliverer(
        app.state.session_factory, settings=settings, senders={"dingtalk": ConsoleSender()}
    )
    assert await deliverer.process_due_once() == 1
    status, retry_count, _channel, _payload, provider_msg_id = _outbox_rows(
        backend_dsn, seeded_org.org_id
    )[0]
    assert status == "sent"
    assert retry_count == 0
    assert provider_msg_id and provider_msg_id.startswith("console-")


class _FlakySender:
    """总是在发送时抛错的 sender（用于验证重试与 DLQ 路径）。"""

    def __init__(self) -> None:
        """初始化（记录尝试次数便于断言）。"""
        self.calls = 0

    def send(self, payload: dict) -> str | None:  # noqa: ARG002 参数协议需要
        """抛 ``NotificationSendError``（模拟 webhook 持续不可用）。"""
        self.calls += 1
        raise NotificationSendError("模拟投递失败")


async def test_retry_then_dlq_after_max_attempts(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """失败重试：``retry_count`` 递增 + 退避；达到上限转 DLQ（不再无限重试）。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    await WorkflowResultConsumer(
        app.state.session_factory, settings=settings, notify_channels=("dingtalk",)
    ).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=SNAPSHOT,
        )
    )
    sender = _FlakySender()
    deliverer = OutboxDeliverer(
        app.state.session_factory,
        settings=settings,
        senders={"dingtalk": sender},
        max_attempts=3,
        backoff_seconds=30,
    )
    base = datetime.now(timezone.utc)
    assert await deliverer.process_due_once(now=base) == 1
    status, retry_count, _c, _p, _m = _outbox_rows(backend_dsn, seeded_org.org_id)[0]
    assert (status, retry_count) == ("pending", 1)

    # 未到下次重试时间：不会再发送
    assert await deliverer.process_due_once(now=base + timedelta(seconds=5)) == 0
    # 到点后重试第 2 次
    assert await deliverer.process_due_once(now=base + timedelta(seconds=31)) == 1
    assert _outbox_rows(backend_dsn, seeded_org.org_id)[0][1] == 2
    # 第 3 次失败 → 达到上限，转 DLQ
    assert await deliverer.process_due_once(now=base + timedelta(seconds=200)) == 1
    status, retry_count, _c, _p, _m = _outbox_rows(backend_dsn, seeded_org.org_id)[0]
    assert (status, retry_count) == ("dlq", 3)
    assert sender.calls == 3
    # DLQ 后不再被领取
    assert await deliverer.process_due_once(now=base + timedelta(seconds=9999)) == 0


async def test_channel_without_sender_goes_dlq(app, settings, backend_dsn: str, seeded_org: SeededOrg):
    """有渠道名但没有 sender（live 模式缺凭据）：直接 DLQ 并告警，而不是无脑重试。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    await WorkflowResultConsumer(
        app.state.session_factory, settings=settings, notify_channels=("dingtalk",)
    ).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=SNAPSHOT,
        )
    )
    deliverer = OutboxDeliverer(app.state.session_factory, settings=settings, senders={})
    assert await deliverer.process_due_once() == 1
    assert _outbox_rows(backend_dsn, seeded_org.org_id)[0][0] == "dlq"


# ---------------------------------------------------------------- 渠道解析


def test_resolve_channels_console_mode_defaults_to_dingtalk(settings):
    """console 模式：即便没配 webhook 也返回钉钉渠道（用控制台 sender 跑通全链路）。"""
    assert resolve_channels(settings) == ("dingtalk",)
    assert isinstance(build_senders(settings, ("dingtalk",))["dingtalk"], ConsoleSender)


def test_resolve_channels_live_mode_requires_credentials(monkeypatch):
    """live 模式：没配 webhook → 空渠道（不出站）；配了才启用。"""
    from pa_backend.core.config import Settings

    monkeypatch.setenv("NOTIFY_MODE", "live")
    monkeypatch.setenv("NOTIFY_DINGTALK_WEBHOOK_URL", "")
    assert resolve_channels(Settings()) == ()
    assert build_senders(Settings(), ()) == {}

    monkeypatch.setenv("NOTIFY_DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=fake")
    assert resolve_channels(Settings()) == ("dingtalk",)
    senders = build_senders(Settings(), ("dingtalk",))
    assert type(senders["dingtalk"]).__name__ == "DingTalkSender"
