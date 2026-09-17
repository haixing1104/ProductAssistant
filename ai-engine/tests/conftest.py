"""ai-engine 集成测试夹具：一次性容器里的真实 PostgreSQL（绝不碰宿主机库）。

数据来源与生命周期:
    · 由 infra/docker-compose.ai-test.yml 起 `pgsql-test`（postgres:17，无数据卷）；
    · 本文件用超级用户 DSN 把 database/sql/*.sql 灌进临时库（角色/schema/表/授权/种子）；
    · 用例级夹具 `seed_business_data` 造真实业务数据并在用例结束后逆序清理，
      最终随 `docker compose ... down -v` 整体销毁。

角色分工（与 database/sql/0002_roles_grants.sql 一致，也是被验证的红线）:
    role_pa_backend —— 造 organizations / sys_users / products / hitl_approvals
        （扮演 backend：这些表只有 backend 能写）；
    role_pa_ai      —— 造 schema_pa_ai.product_contents / evaluation_logs
        （AI 域数据归 ai-engine 自己写），并作为被测工具取数的角色。

环境变量（由 compose 注入；缺失则相关用例自动 skip，便于宿主机无 PG 时跑单测）:
    PA_TEST_SUPERUSER_DSN   超级用户 DSN（灌 schema 用）
    PA_TEST_PG_DSN          role_pa_ai 运行期 DSN（工具取数用）
    PA_TEST_BACKEND_PG_DSN  role_pa_backend DSN（造业务数据用）
    PA_TEST_REDIS_URL       Redis（worker 端到端用例用；缺失则该用例 skip）
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
AI_ENGINE_DIR = TESTS_DIR.parent
REPO_ROOT = AI_ENGINE_DIR.parent
SQL_DIR = REPO_ROOT / "database" / "sql"

# 与 database/tests/conftest.py 对齐：SQL 里的 psql 变量 :'xxx_pwd' 替换为字面量
ROLE_PASSWORDS = {
    "admin_pwd": os.environ.get("ROLE_PA_ADMIN_PWD", "pa_admin_pwd123"),
    "backend_pwd": os.environ.get("ROLE_PA_BACKEND_PWD", "pa_backend_pwd123"),
    "ai_pwd": os.environ.get("ROLE_PA_AI_PWD", "pa_ai_pwd123"),
    "ai_setup_pwd": os.environ.get("ROLE_PA_AI_SETUP_PWD", "pa_ai_setup_pwd123"),
}

_PSQL_VAR_RE = re.compile(r":'(\w+)'")


def _require_dsn(env_var: str, purpose: str) -> str:
    """读取集成测试 DSN；未配置时 skip 当前用例（而不是失败）。

    参数:
        env_var: 环境变量名（如 PA_TEST_PG_DSN）。
        purpose: 该 DSN 的用途（用于 skip 提示）。
    返回:
        DSN 字符串（已去除首尾空白）。
    """
    value = os.environ.get(env_var, "").strip()
    if not value:
        pytest.skip(f"未设置 {env_var}（{purpose}），跳过真实库集成用例")
    return value


def _exec(dsn: str, sql: str, params: tuple = ()) -> None:
    """执行一条语句（autocommit）。

    参数:
        dsn: 目标库 DSN。
        sql: SQL 文本（可含 %s 占位符）。
        params: 参数元组。
    返回:
        无返回值。
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql, params)


def _render_psql_vars(text: str, conn) -> str:
    """把 SQL 文本里的 :'name' 替换为对应密码的 SQL 字面量。

    参数:
        text: 原始 SQL 文本（database/sql/*.sql 的内容）。
        conn: 用于安全转义的连接（psycopg.sql.Literal）。
    返回:
        可执行 SQL 文本。
    注意:
        与 database/tests/conftest.py 同源同构；此处不 import 对方模块，
        避免为了共用工具函数去改那套已通过的 102 例套件。
    """
    from psycopg import sql

    def _repl(match: "re.Match[str]") -> str:
        """单个占位符替换为密码字面量。

        参数:
            match: 命中的 :'name' 正则匹配。
        返回:
            转义后的 SQL 字面量文本。
        异常:
            KeyError: 引用了未知变量名（避免「密码为空」这类静默错误）。
        """
        name = match.group(1)
        if name not in ROLE_PASSWORDS:
            raise KeyError(f"SQL 引用了未知变量 :'{name}'")
        return sql.Literal(ROLE_PASSWORDS[name]).as_string(conn)

    return _PSQL_VAR_RE.sub(_repl, text)


@dataclass
class SeededData:
    """一组真实业务数据的 ID 集合（供用例断言与清理）。"""

    org_id: str
    user_id: str
    product_id: str
    thread_id: str


@pytest.fixture(scope="session")
def pg_superuser_dsn() -> str:
    """超级用户 DSN（灌入 database/sql 初始化脚本用）。

    返回:
        DSN 字符串。
    """
    return _require_dsn("PA_TEST_SUPERUSER_DSN", "灌入 database/sql 初始化脚本")


@pytest.fixture(scope="session")
def ai_dsn(pg_superuser_dsn: str) -> str:
    """role_pa_ai 运行期 DSN（Agent 工具取数用；依赖 schema 已灌好）。

    参数:
        pg_superuser_dsn: 超级用户 DSN（本夹具据此保证 schema 就绪）。
    返回:
        DSN 字符串。
    """
    _apply_schema(pg_superuser_dsn)
    return _require_dsn("PA_TEST_PG_DSN", "role_pa_ai 工具取数")


@pytest.fixture(scope="session")
def backend_dsn(pg_superuser_dsn: str) -> str:
    """role_pa_backend DSN（造 business 域业务数据用；扮演 backend）。

    参数:
        pg_superuser_dsn: 超级用户 DSN（保证 schema 就绪）。
    返回:
        DSN 字符串。
    """
    _apply_schema(pg_superuser_dsn)
    return _require_dsn("PA_TEST_BACKEND_PG_DSN", "role_pa_backend 造业务数据")


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis 连接串（worker 端到端用例用；未配置则 skip 该用例）。

    返回:
        redis:// 连接串。
    """
    return _require_dsn("PA_TEST_REDIS_URL", "worker 端到端用例")


_SCHEMA_APPLIED: set[str] = set()


def _apply_schema(superuser_dsn: str) -> None:
    """按文件名顺序把 database/sql/*.sql 灌入临时库（每个 DSN 只做一次；已初始化则跳过）。

    参数:
        superuser_dsn: 超级用户 DSN。
    返回:
        无返回值。
    注意:
        · 0001_schema.sql 的 `CREATE ROLE` **非幂等**，因此先探测 role_pa_admin 是否已存在：
          已存在即认为该库已初始化过，直接跳过（与 infra/scripts/pgsql-setup.sh 的守卫一致）；
        · 只对**临时容器库**使用；宿主机库（productassistant）绝不调用本函数。
    """
    import psycopg

    if superuser_dsn in _SCHEMA_APPLIED:
        return
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        initialized = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'role_pa_admin'"
        ).fetchone()
        if not initialized:
            for path in sorted(SQL_DIR.glob("*.sql")):
                conn.execute(_render_psql_vars(path.read_text(encoding="utf-8"), conn))
    _SCHEMA_APPLIED.add(superuser_dsn)


@pytest.fixture
def seed_business_data(ai_dsn: str, backend_dsn: str):
    """造一组真实业务数据（org / user / product / contents / logs / approvals），用完即清。

    参数:
        ai_dsn: role_pa_ai DSN（写 schema_pa_ai 的 product_contents / evaluation_logs）。
        backend_dsn: role_pa_backend DSN（写 schema_pa_backend 的 org / user / products / approvals）。
    产出:
        SeededData：四个业务 ID（org/user/product/thread）。
    注意:
        角色分工本身就是红线的一部分：Backend 域表只有 backend 能写、
        AI 域表由 role_pa_ai 写；清理时按「先子表后父表」逆序删除。
    """
    from psycopg.types.json import Jsonb

    data = SeededData(
        org_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        product_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
    )

    # ---- Backend 域（role_pa_backend 写入，扮演 backend）----
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name) VALUES (%s, %s)",
        (data.org_id, "集成测试组织"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users (id, org_id, username, hashed_password, role) "
        "VALUES (%s, %s, %s, %s, %s)",
        (data.user_id, data.org_id, f"tester_{data.user_id[:8]}", "not-a-real-hash", "admin"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.products "
        "(id, org_id, sku_code, title, base_price, stock_status, status, owner_id, raw_images) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            data.product_id,
            data.org_id,
            f"SKU-{data.product_id[:8]}",
            "集成测试商品·真空保温杯",
            19.9,
            "in_stock",
            "draft",
            data.user_id,
            Jsonb([{"url": "https://example.test/cover.png"}]),
        ),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.hitl_approvals "
        "(org_id, product_id, thread_id, approver_id, status, channel, feedback, resolved_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
        (data.org_id, data.product_id, data.thread_id, data.user_id, "rejected", "web", "价格描述需与素材一致"),
    )

    # ---- AI 域（role_pa_ai 写入：这两张表 backend 只读）----
    for version in (1, 2):
        _exec(
            ai_dsn,
            "INSERT INTO schema_pa_ai.product_contents "
            "(org_id, product_id, thread_id, version, content_data, model_name, is_approved) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                data.org_id,
                data.product_id,
                data.thread_id,
                version,
                # content_data 真实形状 = {"blocks": [...]}（见 PgContentStore.save），
                # 种子按真实形状写入，保证工具解析走的是生产同款结构
                Jsonb({"blocks": [{"type": "text", "text": f"第 {version} 版文案：真空保温杯 19.9 元"}]}),
                "glm-4-flash",
                False,
            ),
        )
    _exec(
        ai_dsn,
        "INSERT INTO schema_pa_ai.evaluation_logs "
        "(org_id, product_id, thread_id, evaluator_type, score, errors) VALUES (%s, %s, %s, %s, %s, %s)",
        (data.org_id, data.product_id, data.thread_id, "rule", 60.0, Jsonb(["命中违禁词「国家级」"])),
    )

    yield data

    # ---- 逆序清理（先子表后父表）----
    _exec(ai_dsn, "DELETE FROM schema_pa_ai.evaluation_logs WHERE org_id = %s AND product_id = %s", (data.org_id, data.product_id))
    _exec(ai_dsn, "DELETE FROM schema_pa_ai.product_contents WHERE org_id = %s AND product_id = %s", (data.org_id, data.product_id))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.hitl_approvals WHERE org_id = %s AND product_id = %s", (data.org_id, data.product_id))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE id = %s", (data.product_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.sys_users WHERE id = %s", (data.user_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.organizations WHERE id = %s", (data.org_id,))
