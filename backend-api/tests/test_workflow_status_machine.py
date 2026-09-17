"""``result:workflow`` 状态机用例：四种映射 + 五条幂等/边界加固。

为什么这些断言值得写:
    状态机是 backend 与 ai-engine 之间**唯一**的业务闭环。映射写错、少一条守卫，
    功能测试（「生成完成了」）依然会绿，但线上会出现：状态卡死、审批单重复、
    已批准的商品被打回、删除的商品被复活 —— 全是难排查且带资损/合规风险的问题。

用例与 ``WorkflowResultConsumer`` 的加固编号一一对应（①~⑤ 见其模块 docstring）。
"""

from __future__ import annotations

import uuid

from pa_backend.services.workflow_result_consumer import WorkflowResultConsumer
from tests.conftest import SeededOrg, _exec, result_payload, seed_job


def _consumer(app, settings, *, channels: tuple[str, ...] = ()) -> WorkflowResultConsumer:
    """构造消费器（不启动循环：用例只调 ``process_payload`` 单步推进）。"""
    return WorkflowResultConsumer(app.state.session_factory, settings=settings, notify_channels=channels)


def _product_row(backend_dsn: str, product_id: str) -> tuple[str, str | None]:
    """读商品（status, active_thread_id）。"""
    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, active_thread_id FROM schema_pa_backend.products WHERE id = %s",
            (product_id,),
        ).fetchone()
    return (row[0], str(row[1]) if row[1] else None)


def _job_status(backend_dsn: str, thread_id: str) -> str:
    """读任务状态。"""
    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT status FROM schema_pa_backend.generation_jobs WHERE thread_id = %s", (thread_id,)
        ).fetchone()[0]


def _job_error(backend_dsn: str, thread_id: str) -> str | None:
    """读任务失败原因（商品详情页「最近一次任务失败原因」的数据源）。"""
    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT error FROM schema_pa_backend.generation_jobs WHERE thread_id = %s", (thread_id,)
        ).fetchone()[0]


def _count(backend_dsn: str, table: str, column: str, value: str) -> int:
    """统计行数（用于「不该建单/不该删行」这类反向断言）。"""
    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        return conn.execute(
            f"SELECT count(*) FROM schema_pa_backend.{table} WHERE {column} = %s", (value,)
        ).fetchone()[0]


async def test_published_maps_to_published_and_clears_active_thread(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """``published`` → 商品 published + 任务 succeeded + 清空 active_thread_id。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _product_row(backend_dsn, seeded_org.product_id) == ("published", None)
    assert _job_status(backend_dsn, thread_id) == "succeeded"


async def test_awaiting_human_creates_pending_approval_with_snapshot(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """``awaiting_human`` → 商品 waiting_approval + 任务 waiting_input + 建待审单（带快照）。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    snapshot = {
        "reason": "high_value",
        "content": {"blocks": [{"type": "text", "text": "待审文案"}]},
        "evaluation_result": {"passed": True, "score": 91.0, "errors": []},
        "evaluation_attempts": 1,
    }
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot=snapshot,
        )
    )
    status, active_thread = _product_row(backend_dsn, seeded_org.product_id)
    assert status == "waiting_approval"
    assert active_thread == thread_id  # 待审期间线程仍绑定（审批要 resume 它）
    assert _job_status(backend_dsn, thread_id) == "waiting_input"

    import psycopg

    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, content_snapshot FROM schema_pa_backend.hitl_approvals WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1]["reason"] == "high_value"
    assert row[1]["content"]["blocks"][0]["text"] == "待审文案"


async def test_rejected_and_failed_both_return_to_draft(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """``rejected`` / ``failed`` 都回 draft + 任务 failed + 清空线程。"""
    for result in ("rejected", "failed"):
        thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
        await _consumer(app, settings).process_payload(
            result_payload(
                thread_id=thread_id,
                product_id=seeded_org.product_id,
                org_id=seeded_org.org_id,
                result=result,
            )
        )
        assert _product_row(backend_dsn, seeded_org.product_id) == ("draft", None)
        assert _job_status(backend_dsn, thread_id) == "failed"


# ------------------------------------------------------------ 失败原因回传（可观测性）


async def test_failed_result_carries_error_into_job(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
) -> None:
    """``failed`` 结果里的 ``error`` 必须落进 ``generation_jobs.error``。

    为什么重要（2026-09 实测）: 商品详情页读的是 ``products_router.active_job_error =
    job.error``，而消费器原先从不写该列 —— 任何失败（合规拦截 / LLM 异常）在界面上
    都表现为「商品莫名回到 draft」，根因完全不可见（与「配图静默降级」同类缺陷）。
    """
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    reason = "input_compliance_blocked(商品标题): 命中违禁词「最便宜」（广告法种子）"
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="failed",
            error=reason,
        )
    )
    assert _job_error(backend_dsn, thread_id) == reason


async def test_published_result_clears_stale_error(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
) -> None:
    """成功终态清空历史失败原因：否则「上一次失败原因」会永久挂在已恢复的商品上。"""
    thread_id = seed_job(backend_dsn, org_id=seeded_org.org_id, product_id=seeded_org.product_id)
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.generation_jobs SET error = '旧失败原因' WHERE thread_id = %s",
        (thread_id,),
    )
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _job_error(backend_dsn, thread_id) is None


# ------------------------------------------------------------ 五条加固


async def test_guard_legacy_deleted_product_is_skipped(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """① 历史 deleted 行：结果整条跳过（不得把该商品复活成 published）。

    软删功能已下线（删除=物理删行），此守卫防的是历史数据/备份恢复数据。
    """
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        product_status="deleted",
    )
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _product_row(backend_dsn, seeded_org.product_id)[0] == "deleted"
    assert _job_status(backend_dsn, thread_id) == "running"  # 连任务也不动


async def test_guard_waiting_input_only_accepts_approval_outcomes(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """② 等待审批期：重复的 ``awaiting_human`` 被忽略（不得把待审单重整一遍）。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    consumer = _consumer(app, settings)
    await consumer.process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot={"reason": "high_value", "content": {"blocks": []}},
        )
    )
    assert _job_status(backend_dsn, thread_id) == "waiting_input"
    assert _count(backend_dsn, "hitl_approvals", "thread_id", thread_id) == 0  # 未建单

    # 审批放行后的 published 才被接受
    await consumer.process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _job_status(backend_dsn, thread_id) == "succeeded"
    assert _product_row(backend_dsn, seeded_org.product_id) == ("published", None)


async def test_guard_stale_thread_only_closes_its_own_job(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """③ 旧线程晚到：只收口旧 job，不改当前商品状态（用户点两次生成的场景）。"""
    stale_thread = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        bind_active_thread=False,
    )
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.products SET active_thread_id = %s, status = 'generating' WHERE id = %s",
        (str(uuid.uuid4()), seeded_org.product_id),
    )
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=stale_thread,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _job_status(backend_dsn, stale_thread) == "succeeded"
    assert _product_row(backend_dsn, seeded_org.product_id)[0] == "generating"  # 商品不受影响


async def test_guard_terminal_job_is_idempotent(app, settings, backend_dsn: str, seeded_org: SeededOrg):
    """④ 已终态任务：重复消息直接跳过（不重复改状态、不重复建单）。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.generation_jobs SET status = 'failed' WHERE thread_id = %s",
        (thread_id,),
    )
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="published",
        )
    )
    assert _job_status(backend_dsn, thread_id) == "failed"  # 不变
    assert _product_row(backend_dsn, seeded_org.product_id)[0] == "waiting_approval"  # 不变


async def test_guard_unknown_result_and_unknown_thread_are_dropped(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """⑤ 未知 result / 未知线程：丢弃且不抛异常（避免毒消息把消费循环打死）。"""
    consumer = _consumer(app, settings)
    assert (
        await consumer.process_payload({"result": "brand_new_state", "thread_id": str(uuid.uuid4())}) is True
    )
    assert (
        await consumer.process_payload(
            result_payload(
                thread_id=str(uuid.uuid4()),
                product_id=seeded_org.product_id,
                org_id=seeded_org.org_id,
                result="published",
            )
        )
        is True
    )


async def test_approval_already_decided_is_not_recreated(
    app, settings, backend_dsn: str, seeded_org: SeededOrg
):
    """已定案单不被重复创建（同一线程只有一条审批记录 —— 审批中心不会出现两条待办）。"""
    thread_id = seed_job(
        backend_dsn,
        org_id=seeded_org.org_id,
        product_id=seeded_org.product_id,
        job_status="waiting_input",
        product_status="waiting_approval",
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals (org_id, product_id, thread_id, status, channel) "
        "VALUES (%s, %s, %s, 'approved', 'web')",
        (seeded_org.org_id, seeded_org.product_id, thread_id),
    )
    # 把 job 退回 running 再喂 awaiting_human（模拟乱序/重投）
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.generation_jobs SET status = 'running' WHERE thread_id = %s",
        (thread_id,),
    )
    await _consumer(app, settings).process_payload(
        result_payload(
            thread_id=thread_id,
            product_id=seeded_org.product_id,
            org_id=seeded_org.org_id,
            result="awaiting_human",
            content_snapshot={"reason": "high_value"},
        )
    )
    assert _count(backend_dsn, "hitl_approvals", "thread_id", thread_id) == 1
