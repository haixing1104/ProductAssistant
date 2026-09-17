"""PostgresSaver 集成测试（真实 PG；未配置 PA_TEST_PG_DSN 时自动 skip）。

覆盖生产 PG 方案的四件事：
  ① ensure_checkpoint_schema()（role_pa_ai_setup 语义）幂等建出 LangGraph 四张表；
  ② new_pg_checkpointer()（PostgresSaver + psycopg_pool）能真实落库并支撑 interrupt/resume，
     且写入落在 schema_pa_ai（证明连接级 search_path 生效）；
  ③ 角色红线：role_pa_ai 只能读写 checkpoint 表，不能在 schema_pa_ai 建表（建表权归 setup 角色）；
  ④ delete_thread() 能清掉线程级 checkpoint（purge 联动清理的基础），close_pg_checkpointer()
     能释放连接池。

运行方式（DSN 必须指向**专用测试库**，勿指生产库）：
    cd ai-engine
    PA_TEST_PG_DSN='postgresql://role_pa_ai:<pwd>@127.0.0.1:5432/productassistant_test' \\
    PA_TEST_PG_SETUP_PG_DSN='postgresql://role_pa_ai_setup:<pwd>@127.0.0.1:5432/productassistant_test' \\
      python -m pytest tests/test_pg_checkpointer.py -q
仅给 PA_TEST_PG_DSN 时，建表与读写都用它（需该角色同时具备 CREATE 与 DML）。
也可指向 infra/docker-compose.test.yml 起的临时库（按 compose 网络调整 host/port）。
"""

from __future__ import annotations

import os
import uuid

import pytest

# 依赖缺失（未装 psycopg/连接池/PostgresSaver）时优雅跳过，而不是报 ImportError
psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("langgraph.checkpoint.postgres")

from src.adapters.pg_store import CHECKPOINT_SCHEMA, ensure_checkpoint_schema  # noqa: E402
from src.workflowcore.graph import (  # noqa: E402
    build_workflow,
    close_pg_checkpointer,
    new_pg_checkpointer,
)

# 运行期 DSN（role_pa_ai：DML）；建表 DSN（role_pa_ai_setup：CREATE）缺省时复用前者
TEST_DSN = os.getenv("PA_TEST_PG_DSN", "").strip()
SETUP_DSN = os.getenv("PA_TEST_PG_SETUP_PG_DSN", "").strip() or TEST_DSN

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
HIGH_VALUE_INFO = {"title": "高价商品", "base_price": 600}

# LangGraph PostgresSaver.setup() 迁移出的表（schema_pa_ai 下）
EXPECTED_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="未设置 PA_TEST_PG_DSN（指向专用测试库的 DSN），跳过 PostgresSaver 集成用例",
)


def _connect(dsn: str):
    """打开 autocommit 连接（DDL 与信息查询均需立即生效）。"""
    return psycopg.connect(dsn, autocommit=True)


def _ensure_schema_ready() -> None:
    """确保 checkpoint 所在 schema 存在（缺失且无建 schema 权限时 skip 并给出指引）。

    生产/schema 由 database/sql/0001_schema.sql 建好；本地临时库可能没有 schema，
    此时若 DSN 角色无 CREATE 权限，直接跳过而不是伪装成功。
    """
    with _connect(SETUP_DSN) as conn:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (CHECKPOINT_SCHEMA,),
        ).fetchone()
        if exists:
            return
        try:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
        except psycopg.errors.InsufficientPrivilege as exc:  # pragma: no cover - 环境相关
            pytest.skip(
                f"{CHECKPOINT_SCHEMA} 不存在且建表 DSN 无建 schema 权限；"
                f"请先执行 database/sql 初始化（或指向已初始化的测试库）：{exc}"
            )


def _count_checkpoints(thread_id: str) -> int:
    """统计某线程在 schema_pa_ai.checkpoints 的行数（清理断言用）。"""
    with _connect(TEST_DSN) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {CHECKPOINT_SCHEMA}.checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return int(row[0])


def test_ensure_checkpoint_schema_creates_langgraph_tables():
    """建表幂等：首次创建 + 重复调用不报错，schema_pa_ai 下四张表齐全。"""
    _ensure_schema_ready()

    ensure_checkpoint_schema(SETUP_DSN)
    ensure_checkpoint_schema(SETUP_DSN)  # 幂等：可重复执行

    with _connect(SETUP_DSN) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (CHECKPOINT_SCHEMA,),
        ).fetchall()
    assert EXPECTED_TABLES <= {r[0] for r in rows}


def test_pg_checkpointer_resumes_hitl_and_delete_thread_cleans_up():
    """真实 PG 上的 interrupt/resume 往返 + delete_thread 清理（角色分工：setup 建、runtime 用）。"""
    _ensure_schema_ready()
    ensure_checkpoint_schema(SETUP_DSN)

    saver = new_pg_checkpointer(TEST_DSN, schema=CHECKPOINT_SCHEMA)
    thread_id = str(uuid.uuid4())
    try:
        wf = build_workflow(checkpointer=saver, max_retries=2)
        config = wf.thread_config(thread_id)
        state = {
            "thread_id": thread_id,
            "product_id": PRODUCT_ID,
            "org_id": ORG_ID,
            "raw_product_info": HIGH_VALUE_INFO,
        }

        out = wf.invoke(state, config=config)
        assert "__interrupt__" in out, "高价商品应挂起等待人工审批"

        # 关键：checkpoint 已真实落库（证明 search_path 生效写进 schema_pa_ai，
        # 且运行期角色对 setup 角色新建的表具备 DML——DEFAULT PRIVILEGES 生效）
        assert _count_checkpoints(thread_id) >= 1

        resumed = wf.resume({"approved": True, "feedback": "放行"}, config=config)
        assert resumed.get("status") == "succeeded"

        # purge 联动清理的基础能力
        saver.delete_thread(thread_id)
        assert _count_checkpoints(thread_id) == 0
    finally:
        close_pg_checkpointer(saver)


def test_pg_checkpointer_pool_close_is_idempotent():
    """连接池释放：close 后池关闭，重复 close 不抛异常（优雅停机要求）。"""
    _ensure_schema_ready()
    ensure_checkpoint_schema(SETUP_DSN)

    saver = new_pg_checkpointer(TEST_DSN, schema=CHECKPOINT_SCHEMA, min_size=1, max_size=2)
    assert saver.conn.closed is False

    close_pg_checkpointer(saver)
    close_pg_checkpointer(saver)  # 幂等
    assert saver.conn.closed is True


def test_runtime_role_cannot_create_checkpoint_tables():
    """权限红线：运行期角色（role_pa_ai）不得在 schema_pa_ai 建表（建表权仅 role_pa_ai_setup）。"""
    _ensure_schema_ready()
    with _connect(TEST_DSN) as conn:
        user = conn.execute("SELECT current_user").fetchone()[0]
        if user != "role_pa_ai":
            pytest.skip(f"运行期 DSN 角色为 {user}（非 role_pa_ai），不适用该红线断言")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"CREATE TABLE {CHECKPOINT_SCHEMA}.pa_should_not_exist (id int)")

