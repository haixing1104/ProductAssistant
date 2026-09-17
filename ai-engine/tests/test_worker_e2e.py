"""worker 端到端集成测试（一次性容器里的真实 PG + Redis）。

【验证什么】
    在没有 backend-api 的情况下，用「与 backend 完全相同的投递契约」
    （RedisStreams 写 `pa:{env}:job:generate`）驱动一次完整生成链路，并断言副作用真的落地：
      ① 图跑完（consume_generate_once 返回 done）；
      ② schema_pa_ai.product_contents 出现本次新增行（含 content_data 文本块）；
      ③ schema_pa_ai.evaluation_logs 出现本次评估行；
      ④ evt:{thread_id} 里有过程事件（generate.started / content.chunk / evaluate.result / done）；
      ⑤ result:workflow 里有终态结果 published（backend 消费器据此推进商品状态）。

【数据与生命周期】
    · 业务数据由 tests/conftest.py 在**临时容器库**里造（role_pa_backend 造商品、role_pa_ai 造 AI 域数据）；
    · Redis 用 PA_ENV=test + DB 15，与本地 dev 键空间隔离；
    · 容器随 `docker compose -f infra/docker-compose.ai-test.yml down -v` 整体销毁；
      宿主机 productassistant 库与本地 Redis 数据全程不被触碰。

【运行】
    未设置 PA_TEST_PG_DSN / PA_TEST_REDIS_URL 时自动 skip。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytestmark = pytest.mark.usefixtures("seed_business_data")


def _event_types(client, stream: str) -> list[str]:
    """读取一条流里所有事件的 type（按写入顺序）。

    参数:
        client: redis-py 客户端（decode_responses=True）。
        stream: 完整流键名。
    返回:
        事件 type 列表（无法解析的条目跳过）。
    """
    types: list[str] = []
    for _msg_id, fields in client.xrange(stream):
        raw = fields.get("data")
        if not raw:
            continue
        try:
            types.append(str(json.loads(raw).get("type")))
        except (ValueError, TypeError):
            continue
    return types


def test_worker_consumes_generate_job_end_to_end(ai_dsn, backend_dsn, redis_url, seed_business_data):
    """投递一条 job:generate → 图跑完 → 内容/日志/事件/终态结果全部落地。"""
    redis = pytest.importorskip("redis")
    psycopg = pytest.importorskip("psycopg")
    from src.adapters.redis_eventbus import RedisKeys, RedisStreams
    from src.service.worker import ListingWorker

    env = "test"
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeys(env)
    streams = RedisStreams(client, keys)
    thread_id = str(uuid.uuid4())
    worker = ListingWorker(
        redis_client=client,
        keys=keys,
        runtime_pg_dsn=ai_dsn,
        # 建 checkpoint 表走 setup 角色（与生产同一分工，见 database/sql/0002_roles_grants.sql）
        setup_pg_dsn=os.environ.get("PA_TEST_PG_SETUP_PG_DSN") or None,
        rule_precheck=False,
    )
    try:
        # 与 backend 同一投递路径（信封 data + schema_version）
        streams.xadd(
            keys.job_generate(),
            {
                "thread_id": thread_id,
                "product_id": seed_business_data.product_id,
                "org_id": seed_business_data.org_id,
            },
        )

        outcome = worker.consume_generate_once(block_ms=2000)
        assert outcome == "done", f"图未正常跑完：{outcome}"

        with psycopg.connect(ai_dsn) as conn:
            rows = conn.execute(
                "SELECT version, content_data FROM schema_pa_ai.product_contents "
                "WHERE org_id = %s AND product_id = %s ORDER BY version",
                (seed_business_data.org_id, seed_business_data.product_id),
            ).fetchall()
            log_count = conn.execute(
                "SELECT count(*) FROM schema_pa_ai.evaluation_logs WHERE org_id = %s AND product_id = %s",
                (seed_business_data.org_id, seed_business_data.product_id),
            ).fetchone()[0]

        # 种子里有 2 个版本，本次生成应追加第 3 版；正文为 content_data.blocks 里的 text 块
        assert [row[0] for row in rows] == [1, 2, 3]
        latest_blocks = rows[-1][1]["blocks"]
        assert isinstance(latest_blocks, list) and latest_blocks[0]["type"] == "text"
        assert latest_blocks[0]["text"].strip()
        # 种子 1 条评估 + 本次 1 条
        assert log_count == 2

        event_types = _event_types(client, keys.evt(thread_id))
        assert "generate.started" in event_types
        assert "stage.generating" in event_types
        assert "content.chunk" in event_types
        assert "evaluate.result" in event_types
        assert "done" in event_types

        results = [
            json.loads(fields["data"])
            for _msg_id, fields in client.xrange(keys.workflow_result())
            if fields.get("data") and json.loads(fields["data"]).get("thread_id") == thread_id
        ]
        assert results, "未发布终态结果（result:workflow）"
        assert results[-1]["result"] == "published"
        assert results[-1]["product_id"] == seed_business_data.product_id
    finally:
        worker.close()
        # 清理本次用例写入的 Redis 流（容器销毁是兜底，这里顺手清便于本地反复跑）
        client.delete(keys.evt(thread_id), keys.job_generate())
        client.close()


def test_stale_job_is_reclaimed_and_processed(ai_dsn, redis_url, seed_business_data):
    """P0 回收链路（集成）：worker 崩溃遗留的未 ack 任务能被接管并跑完，且不重复生成。

    复现方式（与真实崩溃等价，且无需真的杀进程）：
      ① 投递一条 `job:generate`；
      ② 用另一个「消费者名」把它读走但**不 ack**（等价于那个 worker 崩溃/被杀）；
      ③ 本 worker 以 `pel_min_idle_ms=0` 立即回收 —— `>` 不会重投已投递消息，
         因此只有 XAUTOCLAIM 能把它捞回来（这正是本用例要证明的边界）。

    断言：跑完 + 该商品只多一版内容（回收不会导致重复生成）+ 不产生死信。
    """
    redis = pytest.importorskip("redis")
    psycopg = pytest.importorskip("psycopg")
    from src.adapters.redis_eventbus import RedisKeys, RedisStreams
    from src.service.worker import ListingWorker

    env = "test"
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeys(env)
    try:
        thread_id = str(uuid.uuid4())
        streams = RedisStreams(client, keys, consumer="producer")
        streams.xadd(
            keys.job_generate(),
            {
                "thread_id": thread_id,
                "product_id": seed_business_data.product_id,
                "org_id": seed_business_data.org_id,
            },
        )
        # ② 模拟崩溃的持有者：读走且不 ack（消息进入 PEL，归属 dead-worker）
        dead = RedisStreams(client, keys, consumer="dead-worker")
        assert dead.read_next(keys.job_generate(), group="ai-engine", block_ms=500) is not None

        # ③ 本 worker 接管跑完
        worker = ListingWorker(
            redis_client=client,
            keys=keys,
            runtime_pg_dsn=ai_dsn,
            setup_pg_dsn=os.environ.get("PA_TEST_PG_SETUP_PG_DSN") or None,
            rule_precheck=False,
            consumer="live-worker",
            pel_min_idle_ms=0,  # 生产用 PEL_MIN_IDLE_MS 控制「多久算滞留」；用例里立即回收
        )
        try:
            assert worker.consume_generate_once(block_ms=2000) == "done"
            assert client.xlen(keys.dlq("job:generate")) == 0, "正常回收不应产生死信"
        finally:
            worker.close()

        # 幂等：种子里 2 版，回收后处理只应多 1 版（不是 2 版）
        with psycopg.connect(ai_dsn) as conn:
            versions = conn.execute(
                "SELECT version FROM schema_pa_ai.product_contents "
                "WHERE org_id = %s AND product_id = %s ORDER BY version",
                (seed_business_data.org_id, seed_business_data.product_id),
            ).fetchall()
        assert [row[0] for row in versions] == [1, 2, 3]
    finally:
        client.delete(keys.job_generate())
        client.close()
