"""backend-api 测试夹具：临时容器库（真实 PostgreSQL/Redis），绝不碰宿主机库。

数据来源与生命周期（与 ai-engine/tests 同一形态）:
    · 由 infra/docker-compose.backend-test.yml 起 ``pgsql-test``（postgres:17，无数据卷）与 ``redis``；
    · 本文件用超级用户 DSN 把 ``database/sql/*.sql`` 灌进临时库（角色/schema/表/授权/种子）；
    · 用例自造数据、用例后清理；最终随 ``docker compose ... down -v`` 整体销毁。

环境变量（由 compose 注入；缺失则相关用例自动 skip，便于本地只跑单测）:
    PA_TEST_SUPERUSER_DSN    超级用户 DSN（灌 schema 用）
    PA_TEST_BACKEND_PG_DSN   role_pa_backend DSN（被测应用连库用；扮演 backend）
    PA_TEST_REDIS_URL        Redis（探针/投递用例用）

关于 DSN 的两套写法（容易踩）:
    · **psycopg 原生**（``database/tests``、``ai-engine/tests`` 用）：``postgresql://…``；
    · **SQLAlchemy async**（本模块用）：``postgresql+psycopg://…``。
    夹具用 ``_to_sqlalchemy_dsn`` 统一给带 ``+psycopg``，避免同一份 .env 要写两种 DSN。
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
SQL_DIR = REPO_ROOT / "database" / "sql"

# 与 database/tests/conftest.py 对齐：SQL 里的 psql 变量 :'xxx_pwd' 替换为字面量
ROLE_PASSWORDS = {
    "admin_pwd": os.environ.get("ROLE_PA_ADMIN_PWD", "pa_admin_pwd123"),
    "backend_pwd": os.environ.get("ROLE_PA_BACKEND_PWD", "pa_backend_pwd123"),
    "ai_pwd": os.environ.get("ROLE_PA_AI_PWD", "pa_ai_pwd123"),
    "ai_setup_pwd": os.environ.get("ROLE_PA_AI_SETUP_PWD", "pa_ai_setup_pwd123"),
}

_PSQL_VAR_RE = re.compile(r":'(\w+)'")


def _to_sqlalchemy_dsn(raw: str) -> str:
    """把 ``postgresql://`` 归一为 SQLAlchemy async 口径（``postgresql+psycopg://``）。

    参数:
        raw: 原始 DSN（可能已带 ``+psycopg``）。
    返回:
        适用于 ``create_async_engine`` 的 DSN。
    """
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


def _require_dsn(env_var: str, purpose: str) -> str:
    """读取集成测试 DSN；未配置时 skip 当前用例（而不是失败）。

    参数:
        env_var: 环境变量名（如 PA_TEST_BACKEND_PG_DSN）。
        purpose: 该 DSN 的用途（用于 skip 提示）。
    返回:
        DSN 字符串（已去首尾空白）。
    """
    value = os.environ.get(env_var, "").strip()
    if not value:
        pytest.skip(f"未设置 {env_var}（{purpose}），跳过需要真实依赖的用例")
    return value


# --- 进程级环境归一：让 Settings() 在任何用例里都能构造 -------------------------------
# 目的：单测（不连库）与集成测试共用同一个 Settings 构造路径，避免「测试环境变量拼错」这种噪音。
os.environ.setdefault("PA_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-pytest")
if os.environ.get("PA_TEST_BACKEND_PG_DSN"):
    # 显式覆盖（不是 setdefault）：容器里就该用容器库，哪怕宿主机 shell 里已有 BACKEND_PG_DSN
    os.environ["BACKEND_PG_DSN"] = _to_sqlalchemy_dsn(os.environ["PA_TEST_BACKEND_PG_DSN"])
else:
    os.environ.setdefault(
        "BACKEND_PG_DSN", "postgresql+psycopg://role_pa_backend:placeholder@localhost:5432/productassistant"
    )
if os.environ.get("PA_TEST_REDIS_URL"):
    os.environ["REDIS_URL"] = os.environ["PA_TEST_REDIS_URL"]


def _exec(dsn: str, sql: str, params: tuple = ()) -> None:
    """执行一条语句（autocommit）。

    参数:
        dsn: 目标库 DSN（psycopg 原生写法）。
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
        conn: 用于安全转义的连接（``psycopg.sql.Literal``）。
    返回:
        可执行 SQL 文本。
    注意:
        与 database/tests/conftest.py 同源同构；此处刻意不 import 对方模块，
        避免为了共用工具函数去改那套已通过的套件。
    """
    from psycopg import sql

    def _repl(match: re.Match[str]) -> str:
        """单个占位符替换为密码字面量。"""
        name = match.group(1)
        if name not in ROLE_PASSWORDS:
            raise KeyError(f"SQL 引用了未知变量 :'{name}'")
        return sql.Literal(ROLE_PASSWORDS[name]).as_string(conn)

    return _PSQL_VAR_RE.sub(_repl, text)


_SCHEMA_APPLIED: set[str] = set()


def _apply_schema(superuser_dsn: str) -> None:
    """按文件名顺序把 ``database/sql/*.sql`` 灌入临时库（每个 DSN 只做一次）。

    参数:
        superuser_dsn: 超级用户 DSN。
    返回:
        无返回值。
    注意:
        ``0001_schema.sql`` 的 ``CREATE ROLE`` **非幂等** → 先探测 ``role_pa_admin`` 是否存在，
        已存在即视为该库已初始化并跳过（与 infra/scripts/pgsql-setup.sh 的守卫一致）。
    """
    import psycopg

    if superuser_dsn in _SCHEMA_APPLIED:
        return
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        initialized = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'role_pa_admin'").fetchone()
        if not initialized:
            for path in sorted(SQL_DIR.glob("*.sql")):
                conn.execute(_render_psql_vars(path.read_text(encoding="utf-8"), conn))
    _SCHEMA_APPLIED.add(superuser_dsn)


@dataclass
class SeededOrg:
    """一组真实业务数据（org + admin 用户 + 一个商品），供用例断言与清理。"""

    org_id: str
    user_id: str
    product_id: str
    username: str
    password: str


@pytest.fixture(scope="session")
def pg_superuser_dsn() -> str:
    """超级用户 DSN（灌入 database/sql 初始化脚本用）。"""
    return _require_dsn("PA_TEST_SUPERUSER_DSN", "灌入 database/sql 初始化脚本")


@pytest.fixture(scope="session")
def backend_dsn(pg_superuser_dsn: str) -> str:
    """``role_pa_backend`` 的原始 DSN（造数据/红线断言用；psycopg 原生写法）。"""
    _apply_schema(pg_superuser_dsn)
    return _require_dsn("PA_TEST_BACKEND_PG_DSN", "role_pa_backend 造业务数据 + 被测应用连库")


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis 连接串（探针/投递用例用；未配置则 skip 该用例）。"""
    return _require_dsn("PA_TEST_REDIS_URL", "Redis 探针与投递用例")


# ======================= 应用夹具（函数级：每个用例一套 engine/app）=======================

from fastapi import FastAPI  # noqa: E402  (放在夹具区之前会打断上方模块级环境归一的可读性)
from httpx import ASGITransport, AsyncClient  # noqa: E402

from pa_backend.core.config import Settings  # noqa: E402
from pa_backend.core.security import hash_password  # noqa: E402
from pa_backend.main import create_app  # noqa: E402


@pytest.fixture
def settings(redis_url: str) -> Settings:
    """被测应用配置：``PA_ENV=test`` + 临时库 DSN + 容器 Redis。

    参数:
        redis_url: 容器 Redis 连接串（覆盖进程内可能残留的宿主机 REDIS_URL）。
    返回:
        ``Settings`` 实例（**不缓存**：每用例独立，避免用例之间互相污染）。
    """
    os.environ["REDIS_URL"] = redis_url
    return Settings()


@pytest.fixture
def db_ready(pg_superuser_dsn: str, backend_dsn: str) -> None:
    """保证临时库 schema 已灌好（app/client 夹具依赖它，避免「角色不存在 → OperationalError」）。

    为什么要单独一个夹具（而不是让 ``settings`` 依赖 DSN）:
        ``settings`` 是**纯配置**夹具，很多用例只想要一份 Settings；把 Db 依赖塞进去会让
        所有用到它的用例都被迫要求数据库。真正需要「库就绪」的是 app/client —— 由它们依赖本夹具。
    注意:
        ``backend_dsn`` 内部已调用 ``_apply_schema``；这里依赖它只是为了表达顺序与意图，
        并没有额外动作（因此断言 schema 表存在属于冗余，交给真正用到表的用例去做）。
    """
    return None


@pytest.fixture
async def app(settings: Settings, db_ready: None):
    """被测 FastAPI 实例（不跑 lifespan：常驻任务在测试里按需单次调用）。

    参数:
        settings: 应用配置。
        db_ready: 保证临时库 schema 已灌好（见该夹具 docstring）。
    产出:
        ``FastAPI`` 实例；用完释放 engine 连接池。
    """
    application: FastAPI = create_app(settings)
    yield application
    await application.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI):
    """ASGI 直连客户端（不起真实端口：更快，且不会有端口冲突）。

    参数:
        app: FastAPI 实例。
    产出:
        ``httpx.AsyncClient``（``base_url=http://test``）。
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def seeded_org(backend_dsn: str) -> SeededOrg:
    """造一组真实业务数据：组织 + admin 用户 + 一个 draft 商品（用完即清）。

    参数:
        backend_dsn: ``role_pa_backend`` DSN（业务域表只有 backend 能写，夹具同样遵守该红线）。
    产出:
        ``SeededOrg``（含明文口令，供登录用例直接使用）。
    注意:
        清理按「先子表后父表」逆序删除（products → sys_users → organizations），
        避免外键约束报错；``delete_audits`` 无外键但也不留残留。
    """
    org_id, user_id, product_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    username, password = f"admin_{user_id[:8]}", "Pa-Test-Passw0rd!"
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.organizations (id, name, status) VALUES (%s, %s, 'active')",
        (org_id, f"测试组织-{org_id[:8]}"),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.sys_users "
        "(id, org_id, username, hashed_password, role, status) VALUES (%s, %s, %s, %s, 'admin', 'active')",
        (user_id, org_id, username, hash_password(password)),
    )
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.products "
        "(id, org_id, sku_code, title, base_price, stock_status, status, owner_id, raw_images) "
        "VALUES (%s, %s, %s, %s, %s, 'in_stock', 'draft', %s, '[]'::jsonb)",
        (product_id, org_id, f"SKU-{product_id[:8]}", "测试商品·真空保温杯", 199.0, user_id),
    )
    data = SeededOrg(
        org_id=org_id, user_id=user_id, product_id=product_id, username=username, password=password
    )
    yield data
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.generation_jobs WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.hitl_approvals WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.notification_outbox WHERE org_id = %s", (org_id,))
    # delete_audits **刻意不清理**：role_pa_backend 只有 SELECT/INSERT（0004 显式 REVOKE UPDATE/DELETE），
    # 审计表必须只能追加 —— 这里若写 DELETE 会直接被数据库拒绝（实测踩过），
    # 这正是「权限矩阵被真实执行」的证据；相关红线由 test_roles_red_lines.py 锁定。
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.products WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.sys_users WHERE org_id = %s", (org_id,))
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.organizations WHERE id = %s", (org_id,))


@pytest.fixture
def auth_headers(seeded_org: SeededOrg, backend_dsn: str) -> dict[str, str]:
    """直接签发 admin 的 access token（跳过登录流程，供「无需验证登录本身」的用例使用）。

    参数:
        seeded_org: 已造好的组织/用户。
        backend_dsn: 未直接使用，仅用于保证 schema/数据就绪（依赖顺序）。
    返回:
        ``{"Authorization": "Bearer <token>"}``。
    """
    from pa_backend.core.security import create_access_token

    token = create_access_token(
        user_id=seeded_org.user_id,
        org_id=seeded_org.org_id,
        role="admin",
        settings=Settings(),
    )
    return {"Authorization": f"Bearer {token}"}


def seed_job(
    backend_dsn: str,
    *,
    org_id: str,
    product_id: str,
    job_status: str = "running",
    product_status: str = "generating",
    bind_active_thread: bool = True,
) -> str:
    """造一条生成任务并把商品置于指定状态（P4 状态机用例的公共夹具）。

    参数:
        backend_dsn: ``role_pa_backend`` DSN。
        org_id: 组织 ID。
        product_id: 商品 ID。
        job_status: ``generation_jobs.status``（running / waiting_input / succeeded / failed）。
        product_status: ``products.status``（generating / waiting_approval / …）。
        bind_active_thread: 是否把 ``products.active_thread_id`` 指向本线程
            （False = 模拟「旧线程晚到」场景）。
    返回:
        新造的 ``thread_id``。
    """
    thread_id = str(uuid.uuid4())
    _exec(
        backend_dsn,
        "INSERT INTO schema_pa_backend.generation_jobs (thread_id, org_id, product_id, status) "
        "VALUES (%s, %s, %s, %s)",
        (thread_id, org_id, product_id, job_status),
    )
    _exec(
        backend_dsn,
        "UPDATE schema_pa_backend.products SET status = %s, active_thread_id = %s WHERE id = %s",
        (product_status, thread_id if bind_active_thread else None, product_id),
    )
    return thread_id


def result_payload(
    *,
    thread_id: str,
    product_id: str,
    org_id: str,
    result: str,
    content_snapshot: dict | None = None,
) -> dict:
    """构造一条 ``result:workflow`` 载荷（与 ai-engine ``_publish_outcome`` 字段一致）。"""
    payload: dict = {
        "schema_version": 1,
        "thread_id": thread_id,
        "product_id": product_id,
        "org_id": org_id,
        "result": result,
    }
    if content_snapshot is not None:
        payload["content_snapshot"] = content_snapshot
    return payload

