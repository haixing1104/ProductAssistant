"""初始化/迁移测试 —— 验证 database/sql/*.sql 建出的结构完整、约束正确、且可安全重放。

覆盖点：
  · 两个 schema 与全部表存在；
  · 0003 种子条数正确；
  · CHECK / UNIQUE 约束生效；
  · updated_at 触发器生效；
  · 0002 / 0004 可重复执行（幂等）。
"""
from pathlib import Path

import psycopg
import pytest

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# 期望存在的表（来源：0001_schema.sql + 0004_delete_audit.sql）
EXPECTED_TABLES = [
    "schema_pa_backend.organizations",
    "schema_pa_backend.sys_users",
    "schema_pa_backend.products",
    "schema_pa_backend.hitl_approvals",
    "schema_pa_backend.compliance_words",
    "schema_pa_backend.compliance_rules",
    "schema_pa_backend.generation_jobs",
    "schema_pa_backend.notification_outbox",
    "schema_pa_backend.delete_audits",
    "schema_pa_ai.product_contents",
    "schema_pa_ai.evaluation_logs",
]

# 0003_seed.sql 的种子条数
SEED_COUNTS = {
    "schema_pa_backend.compliance_words": 3,   # 违禁词 3 条
    "schema_pa_backend.compliance_rules": 2,   # 合规规则 2 条
}


def test_schemas_exist(superuser):
    """0001 应建出 schema_pa_backend 与 schema_pa_ai 两个 schema。"""
    found = { # 集合推导式
        row[0]
        for row in superuser.execute(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname IN ('schema_pa_backend','schema_pa_ai')"
        ).fetchall()
    }
    assert found == {"schema_pa_backend", "schema_pa_ai"}


def test_expected_tables_exist(superuser):
    """全部业务表/AI 表都应存在（表结构契约）。"""
    existing = {
        row[0]
        for row in superuser.execute(
            "SELECT schemaname || '.' || tablename FROM pg_tables "
            "WHERE schemaname IN ('schema_pa_backend','schema_pa_ai')"
        ).fetchall()
    }
    missing = [t for t in EXPECTED_TABLES if t not in existing]
    assert not missing, f"缺少表: {missing}"


def test_seed_counts(superuser):
    """0003 种子应写入预期条数。"""
    for table, expected in SEED_COUNTS.items():
        actual = superuser.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert actual == expected, f"{table}: 实际 {actual} 期望 {expected}"


def test_check_constraint_rejects_bad_status(superuser):
    """CHECK 约束：organizations.status 只允许 active/suspended，非法值应被拒。"""
    with pytest.raises(psycopg.errors.CheckViolation):
        superuser.execute(
            "INSERT INTO schema_pa_backend.organizations (name, status) "
            "VALUES ('bad-status', 'bogus')"
        )


def test_unique_org_username(superuser):
    """UNIQUE 约束：同一组织内 username 唯一（uq_sys_users_org_username）。"""
    org_id = superuser.execute(
        "INSERT INTO schema_pa_backend.organizations (name) "
        "VALUES ('uq-username') RETURNING id"
    ).fetchone()[0]
    superuser.execute(
        "INSERT INTO schema_pa_backend.sys_users (org_id, username, hashed_password) "
        "VALUES (%s, 'dup', 'h')", (org_id,)
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        superuser.execute(
            "INSERT INTO schema_pa_backend.sys_users (org_id, username, hashed_password) "
            "VALUES (%s, 'dup', 'h')", (org_id,)
        )


def test_unique_org_sku(superuser):
    """UNIQUE 约束：同一组织内 SKU 唯一（uq_products_org_sku）。"""
    org_id = superuser.execute(
        "INSERT INTO schema_pa_backend.organizations (name) "
        "VALUES ('uq-sku') RETURNING id"
    ).fetchone()[0]
    superuser.execute(
        "INSERT INTO schema_pa_backend.products (org_id, sku_code, title) "
        "VALUES (%s, 'SKU1', 't')", (org_id,)
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        superuser.execute(
            "INSERT INTO schema_pa_backend.products (org_id, sku_code, title) "
            "VALUES (%s, 'SKU1', 't')", (org_id,)
        )


def test_updated_at_trigger(superuser):
    """触发器：UPDATE 后 updated_at 应由 DB 自动刷新（set_updated_at 触发器）。"""
    org_id, before = superuser.execute(
        "INSERT INTO schema_pa_backend.organizations (name) "
        "VALUES ('trigger-test') RETURNING id, updated_at"
    ).fetchone()
    after = superuser.execute(
        "UPDATE schema_pa_backend.organizations SET name = 'trigger-test-2' "
        "WHERE id = %s RETURNING updated_at", (org_id,)
    ).fetchone()[0]
    assert after > before, f"updated_at 触发器未生效: {before} -> {after}"


def test_migrations_reapply_is_idempotent(apply_sql_file):
    """幂等：0002（GRANT/默认权限）与 0004（IF NOT EXISTS）重复执行不应报错。"""
    apply_sql_file(SQL_DIR / "0002_roles_grants.sql")
    apply_sql_file(SQL_DIR / "0004_delete_audit.sql")
