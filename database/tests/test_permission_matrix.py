"""权限矩阵实测 —— 把“schema 隔离”这条架构红线变成可执行的测试。

【背景】
  schema_pa_backend 属 backend-api 域，schema_pa_ai 属 ai-engine 域；
  两侧仅通过最小权限互相“只读”访问（见 database/sql/0002_roles_grants.sql）。
  这条红线是“机器可执行的架构约束”：

  1) test_permission_matrix：用 has_table_privilege 对
     “角色 × 表 × 操作(SELECT/INSERT/UPDATE/DELETE)” 做全量核对（88 例）；
  2) 关键红线用例：直接以目标角色连库执行真实 SQL，验证“应通过 / 应被拒”。

【期望来源】
  EXPECTED 依据 database/sql/0002_roles_grants.sql（GRANT + ALTER DEFAULT PRIVILEGES）
  与 0004_delete_audit.sql（对 delete_audits 的 GRANT/REVOKE）；未列出的 (role, table)
  表示“无任何权限”（不授即拒）。
"""
import psycopg
import pytest

# 表级四种 DML 权限
DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}

# 全部被测表（来源：0001_schema.sql 建表 + 0004_delete_audit.sql 补 delete_audits）
TABLES = {
    "schema_pa_backend": [
        "organizations", "sys_users", "products", "hitl_approvals",
        "compliance_words", "compliance_rules", "generation_jobs",
        "notification_outbox", "delete_audits",
    ],
    "schema_pa_ai": ["product_contents", "evaluation_logs"],
}
ALL_TABLES = [f"{schema}.{table}" for schema, tables in TABLES.items() for table in tables]

# 期望权限矩阵：role -> {schema.table -> 允许的权限集合}；未出现的 (role, table) = 无任何权限。
# 依据 0002_roles_grants.sql + 0004_delete_audit.sql。
EXPECTED = {
    # role_pa_backend：backend 域 8 张表全 DML；delete_audits 仅 SELECT/INSERT（0004 已 REVOKE UPDATE/DELETE）；
    #                  对 ai 域的 product_contents/evaluation_logs 仅 SELECT（只读展示）。
    "role_pa_backend": {
        **{
            f"schema_pa_backend.{t}": DML
            for t in [
                "organizations", "sys_users", "products", "hitl_approvals",
                "compliance_words", "compliance_rules", "generation_jobs",
                "notification_outbox",
            ]
        },
        "schema_pa_backend.delete_audits": {"SELECT", "INSERT"},
        "schema_pa_ai.product_contents": {"SELECT"},
        "schema_pa_ai.evaluation_logs": {"SELECT"},
    },
    # role_pa_ai：对 backend 域的 products/hitl_approvals 仅 SELECT（只读素材/审计）；
    #             对 ai 域两张表全 DML；其余表（organizations/sys_users/compliance_*/...）零权限。
    "role_pa_ai": {
        "schema_pa_backend.products": {"SELECT"},
        "schema_pa_backend.hitl_approvals": {"SELECT"},
        "schema_pa_ai.product_contents": DML,
        "schema_pa_ai.evaluation_logs": DML,
    },
}


def _expected(role: str, table: str, priv: str) -> bool:
    """查询期望矩阵：该角色对该表该项权限是否应当具备。"""
    return priv in EXPECTED.get(role, {}).get(table, set())


@pytest.mark.parametrize("role", ["role_pa_backend", "role_pa_ai"])
@pytest.mark.parametrize("table", ALL_TABLES)
@pytest.mark.parametrize("priv", sorted(DML))
def test_permission_matrix(superuser, role, table, priv):
    """全量核对：2 角色 × 11 表 × 4 权限 = 88 例。

    has_table_privilege(role, table, priv) psql内部函数，返回该角色是否具备权限；
    与 EXPECTED 期望逐一比对，任一处偏差（如误加/误删 GRANT）立即失败。
    """
    actual = superuser.execute(
        "SELECT has_table_privilege(%s, %s, %s)", (role, table, priv)
    ).fetchone()[0]
    want = _expected(role, table, priv)
    assert actual == want, f"{role} / {table} / {priv}: 实际={actual} 期望={want}"


# --- 关键红线：以真实角色连库执行，验证“应通过 / 应被拒” ---

def test_ai_can_read_products(ai_dsn):
    """红线：ai-engine 可读商品素材（products 对 ai 仅 SELECT）。"""
    with psycopg.connect(ai_dsn, autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM schema_pa_backend.products").fetchone()


def test_ai_cannot_read_sys_users(ai_dsn):
    """红线：ai-engine 不得读用户表 sys_users（未授权，应 permission denied）。"""
    with psycopg.connect(ai_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM schema_pa_backend.sys_users").fetchone()


def test_ai_cannot_write_products(ai_dsn):
    """红线：ai-engine 绝不写商品基础信息（products 对 ai 只读）。"""
    with psycopg.connect(ai_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE schema_pa_backend.products SET base_price = 1")


def test_backend_can_read_ai_contents(backend_dsn):
    """红线：backend 可读 AI 生成内容（product_contents 仅 SELECT，用于展示/Trace）。"""
    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM schema_pa_ai.product_contents").fetchone()


def test_backend_cannot_write_ai_contents(backend_dsn):
    """红线：backend 不得写 AI 域（product_contents 对 backend 只读，写应由 ai-engine 负责）。"""
    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO schema_pa_ai.product_contents "
                "(org_id, product_id, thread_id, content_data) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), '{}')"
            )


def test_backend_cannot_update_delete_audits(backend_dsn):
    """红线：删除审计表只可追加（INSERT/SELECT），不可改（UPDATE 应被拒）。"""
    with psycopg.connect(backend_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE schema_pa_backend.delete_audits SET reason = reason")
