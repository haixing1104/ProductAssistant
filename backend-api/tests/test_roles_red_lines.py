"""角色红线用例：**权限矩阵是机器可执行的架构约束**（database/sql/0002/0004）。

为什么要断言「被拒绝」:
    权限矩阵的意义就在于**越权写入必须失败**。只断言「正常写入成功」测不出矩阵是否真的生效 ——
    某天有人把 GRANT 放宽（或把代码里的角色换成 admin），功能测试依然全绿，红线却已经没了。

覆盖（与 backend-api/README §六 的红线清单一一对应）:
    ① ``role_pa_backend`` 写 ``schema_pa_ai.product_contents`` 必须被拒（AI 域归 ai-engine）；
    ② ``role_pa_backend`` 对 ``delete_audits`` 的 UPDATE / DELETE 必须被拒（审计只能追加）；
    ③ ``role_pa_backend`` 对 ``job_abort_audits``（0005 运维终止审计）同上。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

#: 权限不足的错误码（PostgreSQL 42501 = insufficient_privilege）
INSUFFICIENT_PRIVILEGE = "42501"


def _expect_denied(dsn: str, sql: str, params: tuple = ()) -> None:
    """执行语句并断言被数据库拒绝（权限不足）。

    参数:
        dsn: 目标库 DSN（psycopg 原生写法）。
        sql: SQL 文本。
        params: 参数元组。
    异常:
        pytest.fail: 语句竟然成功（说明红线已失效）。
    """
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql, params)
    except psycopg.errors.InsufficientPrivilege as exc:
        assert exc.sqlstate == INSUFFICIENT_PRIVILEGE
        return
    except psycopg.Error as exc:  # 其它错误（语法/约束）：不是我们想验证的点，直接暴露出来
        pytest.fail(f"预期权限拒绝，实际抛出 {type(exc).__name__}: {exc}")
    pytest.fail("预期权限拒绝，但语句执行成功 —— 权限矩阵已被放宽（红线失效）")


def test_backend_role_cannot_write_ai_schema(backend_dsn: str, seeded_org):
    """① backend 角色不得写 ``schema_pa_ai``（展示只读，写归 ai-engine）。"""
    _expect_denied(
        backend_dsn,
        "INSERT INTO schema_pa_ai.evaluation_logs "
        "(org_id, product_id, evaluator_type) VALUES (%s, %s, 'llm')",
        (seeded_org.org_id, seeded_org.product_id),
    )


def test_delete_audits_is_append_only(backend_dsn: str, seeded_org):
    """② ``delete_audits`` 只能追加：UPDATE / DELETE 均被拒（否则审计等于没有）。"""
    audit_id = str(uuid.uuid4())
    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO schema_pa_backend.delete_audits "
            "(id, org_id, product_id, sku_code, title, actor_user_id, reason) "
            "VALUES (%s, %s, %s, 'SKU-REDLINE', '红线用例商品', %s, '权限矩阵用例')",
            (audit_id, seeded_org.org_id, seeded_org.product_id, seeded_org.user_id),
        )
    _expect_denied(
        backend_dsn, "UPDATE schema_pa_backend.delete_audits SET reason = '改一下' WHERE id = %s", (audit_id,)
    )
    _expect_denied(backend_dsn, "DELETE FROM schema_pa_backend.delete_audits WHERE id = %s", (audit_id,))


def test_job_abort_audits_is_append_only(backend_dsn: str, seeded_org):
    """③ ``job_abort_audits``（0005）同样只能追加 —— 运维终止任务的记录必须改不掉、删不掉。"""
    audit_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO schema_pa_backend.job_abort_audits "
            "(id, org_id, job_id, thread_id, product_id, actor_user_id, reason) "
            "VALUES (%s, %s, gen_random_uuid(), %s, %s, %s, '权限矩阵用例')",
            (audit_id, seeded_org.org_id, thread_id, seeded_org.product_id, seeded_org.user_id),
        )
    _expect_denied(
        backend_dsn,
        "UPDATE schema_pa_backend.job_abort_audits SET reason = '改一下' WHERE id = %s",
        (audit_id,),
    )
    _expect_denied(
        backend_dsn, "DELETE FROM schema_pa_backend.job_abort_audits WHERE id = %s", (audit_id,)
    )
