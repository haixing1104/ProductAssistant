"""审批历史用例（A）：列表按状态/商品过滤 + 审批人姓名 + 通知投递状态（B）。

为什么需要这个能力（不是「多一个查询参数」那么简单）:
    · 原 ``/approvals/pending`` 只回 ``pending``：审批人点完按钮，那张单就从界面**彻底消失** ——
      「我批过什么、当时驳回原因是什么」只能去查库；质量事故复盘没有入口；
    · 商品详情页要展示「被驳回复盘」也需要按 ``product_id`` 查历史；
    · PA **没有** PP 的 ``last_reject_feedback/last_reject_snapshot`` 冗余列，
      所以这条查询是唯一的数据来源（见根 README 的模块差异表）。

覆盖的边界:
    · 默认行为不变（不传 ``status`` = ``pending``，且与 ``/pending`` 等价）；
    · 已处理单可见且带 ``feedback`` / ``approver_name`` / ``resolved_at``；
    · ``product_id`` 过滤；非法 ``status`` → 400；operator → 403；
    · 每项都带 ``notifications``（无 outbox 行时为 ``[]``，不是缺字段）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.services.workflow_result_consumer import WorkflowResultConsumer
from tests.conftest import SeededOrg, _exec, result_payload, seed_job
from tests.test_approvals import _seed_pending_approval
from tests.test_notifications import SNAPSHOT

APPROVALS_URL = "/api/v1/approvals"
PENDING_URL = "/api/v1/approvals/pending"


def _insert_other_product(backend_dsn: str, *, org_id: str, user_id: str, product_id: str) -> None:
    """造第二个商品（用于验证 product_id 过滤真的生效）。"""
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.products "
        "(id, org_id, owner_id, sku_code, title, base_price, stock_status, status, raw_images) "
        "VALUES (%s, %s, %s, %s, '用于过滤验证的商品', 20.00, 'in_stock', 'waiting_approval', '[]'::jsonb)",
        (product_id, org_id, user_id, f"SKU-FILTER-{product_id[:8]}"),
    )


async def test_decided_approval_is_visible_in_history_with_approver_name(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """驳回后那张单必须能被查回来，并带上「谁批的、为什么驳回」。"""
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    rejected = await client.post(
        f"{APPROVALS_URL}/{approval_id}/reject",
        json={"feedback": "功效表述缺乏依据，请改写"},
        headers=auth_headers,
    )
    assert rejected.status_code == 200, rejected.text

    # 默认（pending）查不到它了 —— 但历史里必须能查到
    default_list = await client.get(APPROVALS_URL, headers=auth_headers)
    assert approval_id not in {item["id"] for item in default_list.json()["data"]}

    history = await client.get(APPROVALS_URL, params={"status": "rejected"}, headers=auth_headers)
    assert history.status_code == 200, history.text
    items = {item["id"]: item for item in history.json()["data"]}
    assert approval_id in items
    item = items[approval_id]
    assert item["status"] == "rejected"
    assert item["feedback"] == "功效表述缺乏依据，请改写"
    assert item["approver_name"] == seeded_org.username  # backend join sys_users 补全
    assert item["approver_id"] is not None and item["resolved_at"] is not None
    assert item["notifications"] == []  # 无 outbox 行：空列表而非缺字段
    assert int(history.headers["X-Total-Count"]) >= 1


async def test_history_filters_by_product_id(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """按 ``product_id`` 过滤：商品详情页的「审批与驳回复盘」据此取数。"""
    other_product_id = str(uuid.uuid4())
    _insert_other_product(
        backend_dsn, org_id=seeded_org.org_id, user_id=seeded_org.user_id, product_id=other_product_id
    )
    try:
        _t1, approval_a = _seed_pending_approval(
            backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
        )
        _t2, approval_b = _seed_pending_approval(
            backend_dsn, org_id=seeded_org.org_id, product_id=other_product_id
        )
        only_a = await client.get(
            APPROVALS_URL,
            params={"product_id": seeded_org.product_id, "limit": 200},
            headers=auth_headers,
        )
        assert only_a.status_code == 200, only_a.text
        ids = {item["id"] for item in only_a.json()["data"]}
        assert approval_a in ids and approval_b not in ids
    finally:
        # 先删子表（hitl_approvals / generation_jobs 对 products.id 有物理外键，未定义级联）
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.hitl_approvals WHERE product_id = %s", (other_product_id,))
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.generation_jobs WHERE product_id = %s", (other_product_id,))
        _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE id = %s", (other_product_id,))


async def test_status_all_returns_decided_approval_for_product(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg
):
    """``status=all`` + ``product_id``：复盘必须看到**已定案**的单（2026-09 事故回归点）。

    事故现场：商品详情页的「审批与驳回复盘」只传 ``product_id``（+ with_snapshot），
    而 backend 缺省语义是 ``status=pending`` → 驳回单永远查不到，界面恒显示
    「该商品还没有审批记录」，于是「驳回后详情页看不到被驳回的图文」。
    这里锁住两件事：``all`` 能取到已定案单且带完整快照；缺省行为**保持不变**。
    """
    _thread, approval_id = _seed_pending_approval(
        backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id
    )
    rejected = await client.post(
        f"{APPROVALS_URL}/{approval_id}/reject",
        json={"feedback": "配图不符，请重做"},
        headers=auth_headers,
    )
    assert rejected.status_code == 200, rejected.text

    # 复盘卡的真实请求形状：product_id + status=all + with_snapshot
    replay = await client.get(
        APPROVALS_URL,
        params={
            "product_id": seeded_org.product_id,
            "status": "all",
            "with_snapshot": "true",
            "limit": 20,
        },
        headers=auth_headers,
    )
    assert replay.status_code == 200, replay.text
    items = {item["id"]: item for item in replay.json()["data"]}
    assert approval_id in items, "已定案单必须出现在「全部状态」的回盘里"
    assert items[approval_id]["status"] == "rejected"
    assert items[approval_id]["feedback"] == "配图不符，请重做"
    assert items[approval_id]["content_snapshot"] is not None, "复盘要能看到被驳回的图文快照"

    # 兼容约定不变：缺省（不传 status）仍是 pending → 查不到这张已定案单
    default_list = await client.get(
        APPROVALS_URL, params={"product_id": seeded_org.product_id}, headers=auth_headers
    )
    assert approval_id not in {item["id"] for item in default_list.json()["data"]}


async def test_status_all_accepted_while_empty_status_still_400(client: AsyncClient, auth_headers: dict):
    """``all`` 是合法过滤值；空串仍是 400（"不传=全部"这种歧义必须被拒绝）。"""
    ok_resp = await client.get(APPROVALS_URL, params={"status": "all", "limit": 1}, headers=auth_headers)
    assert ok_resp.status_code == 200, ok_resp.text

    empty = await client.get(APPROVALS_URL, params={"status": "", "limit": 1}, headers=auth_headers)
    assert empty.status_code == 400
    assert "all" in empty.json()["message"]


async def test_pending_alias_matches_default_list(client: AsyncClient, auth_headers: dict):
    """默认行为不变：不传 ``status`` ≡ ``status=pending`` ≡ ``/approvals/pending``。"""
    default_list = await client.get(APPROVALS_URL, params={"limit": 200}, headers=auth_headers)
    explicit = await client.get(
        APPROVALS_URL, params={"status": "pending", "limit": 200}, headers=auth_headers
    )
    alias = await client.get(PENDING_URL, params={"limit": 200}, headers=auth_headers)
    for resp in (default_list, explicit, alias):
        assert resp.status_code == 200, resp.text
    assert (
        default_list.headers["X-Total-Count"]
        == explicit.headers["X-Total-Count"]
        == alias.headers["X-Total-Count"]
    )
    assert {item["id"] for item in default_list.json()["data"]} == {
        item["id"] for item in alias.json()["data"]
    }


async def test_history_rejects_invalid_status_and_operator(client: AsyncClient, auth_headers: dict):
    """非法 status → 400（入参校验）；operator 不能访问审批面（403）。"""
    bad = await client.get(APPROVALS_URL, params={"status": "whatever"}, headers=auth_headers)
    assert bad.status_code == 400
    assert "status" in bad.json()["message"]

    bad_product = await client.get(APPROVALS_URL, params={"product_id": "not-a-uuid"}, headers=auth_headers)
    assert bad_product.status_code == 404  # to_uuid 口径：非法 id 与不存在同响应

    from tests.test_members_rbac import _create_member, _login

    member = await _create_member(client, auth_headers, "operator")
    headers = {"Authorization": f"Bearer {await _login(client, member['username'], 'Pa-Member-Passw0rd!')}"}
    assert (await client.get(APPROVALS_URL, headers=headers)).status_code == 403
    assert (await client.get(PENDING_URL, headers=headers)).status_code == 403


async def test_notifications_visible_after_delivery_failure(
    client: AsyncClient, auth_headers: dict, settings: Settings, backend_dsn: str, seeded_org: SeededOrg, app
):
    """B：投递失败后，审批接口必须能回答「通知为什么没到」（状态 + 重试次数 + 原因）。

    为什么这条断言重要:
        通知是审批的**触发条件**：没人收到通知 = 审批卡住。而「卡住」与「发出去了但没人处理」
        在界面上通常长得一模一样；只有把投递状态与失败原因暴露出来，运维才能一眼分清
        「配错了 webhook」和「审批人没空」。
    """
    from pa_backend.services.notifications.adapters import NotificationSendError
    from pa_backend.services.notifications.deliverer import OutboxDeliverer

    class _BoomSender:
        """总是失败的 sender（模拟 webhook 填错/网络不可达）。"""

        def send(self, payload: dict) -> str:  # noqa: ARG002
            """模拟真实投递被渠道拒绝（webhook 401）。

            为什么要抛 ``NotificationSendError`` 而不是 ``Exception``:
                这条异常是「渠道明确拒绝」的语义（区别于网络抖动）—— 投递器据此决定
                是否记 ``failed`` 并在前端显示真实原因；抛错类型不对会把断言变成假通过。
            """
            raise NotificationSendError("群机器人 webhook 返回 401 invalid token")

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
    # max_attempts=1 → 一次失败即 DLQ，便于断言终态
    deliverer = OutboxDeliverer(
        app.state.session_factory,
        settings=settings,
        senders={"dingtalk": _BoomSender()},
        max_attempts=1,
    )
    assert await deliverer.process_due_once() == 1

    listing = await client.get(
        APPROVALS_URL, params={"product_id": seeded_org.product_id}, headers=auth_headers
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    assert rows, "应能查到刚建的待审单"
    notes = rows[0]["notifications"]
    assert len(notes) == 1
    assert notes[0]["channel"] == "dingtalk"
    assert notes[0]["status"] == "dlq"
    assert notes[0]["retry_count"] == 1
    assert "invalid token" in (notes[0]["error"] or "")  # 失败原因可读，而不是只有「投递失败」
