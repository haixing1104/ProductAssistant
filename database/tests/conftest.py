"""database/tests/conftest.py — 数据库测试公共夹具（pytest）

【pgsql测试描述】
  由 infra/docker-compose.test.yml 编排两个一次性容器：
    · pgsql-test：官方 postgres:17（临时库，用完即删，绝不碰生产库）
    · tests     ：本套测试（Python + pytest + psycopg）
  宿主机只需要 Docker，无需安装 Python / PostgreSQL。运行：
    docker compose -f infra/docker-compose.test.yml up --build \
      --abort-on-container-exit --exit-code-from tests
    docker compose -f infra/docker-compose.test.yml down -v

【数据流】
  1) 会话开始：以超级用户连上临时库；
  2) 按文件名顺序执行 database/sql/*.sql（0001→0004），建角色/schema/表/授权/种子；
  3) 各测试按需用“不同角色的 DSN”连接，断言权限与约束；
  4) 会话结束：容器随 compose 销毁。

【为什么不需要 psql】
  database/sql/0001_schema.sql 用 psql 变量接收角色密码（如 PASSWORD :'admin_pwd'）。
  本夹具用 psycopg（pgsql桥梁） 把 :'name' 占位符替换为等价的 SQL 字面量后再执行，
  因此测试镜像无需安装 psql（也少一个慢速 apt 依赖）。

【环境变量（由 compose 注入，见 infra/docker-compose.test.yml）】
  PGHOST / PGPORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
      —— 连临时 PostgreSQL 的地址与超级用户（默认库 productassistant_test）
  ROLE_PA_ADMIN_PWD / ROLE_PA_BACKEND_PWD / ROLE_PA_AI_PWD / ROLE_PA_AI_SETUP_PWD
      —— 0001 创建 4 个角色时写入的密码；测试用它们以对应角色登录做权限断言
"""
import os
import re
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

# --- 路径：相对本文件定位，容器内在 /work/database 下同样成立 ---
TESTS_DIR = Path(__file__).resolve().parent      # .../database/tests
SQL_DIR = TESTS_DIR.parent / "sql"               # .../database/sql

# --- 连接参数（默认值仅便于本地直跑；CI/容器由 compose 注入真实值）---
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("POSTGRES_USER", "postgres")
PGPASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
DBNAME = os.environ.get("POSTGRES_DB", "productassistant_test")

# 角色密码映射：键名必须与 database/sql/0001_schema.sql 里的 :'xxx' 变量名一致
# （admin_pwd / backend_pwd / ai_pwd / ai_setup_pwd），否则 _render() 会报未知变量。
ROLE_PASSWORDS = {
    "admin_pwd": os.environ.get("ROLE_PA_ADMIN_PWD", "pa_admin_pwd123"),
    "backend_pwd": os.environ.get("ROLE_PA_BACKEND_PWD", "pa_backend_pwd123"),
    "ai_pwd": os.environ.get("ROLE_PA_AI_PWD", "pa_ai_pwd123"),
    "ai_setup_pwd": os.environ.get("ROLE_PA_AI_SETUP_PWD", "pa_ai_setup_pwd123"),
}

# 匹配 psql 变量占位符 :'name'（单引号包裹），用于替换为 SQL 字面量
_PSQL_VAR_RE = re.compile(r":'(\w+)'")


def dsn(user: str, password: str) -> str:
    """拼 PostgreSQL 连接串（psycopg 可直接 connect）。"""
    return f"postgresql://{user}:{password}@{PGHOST}:{PGPORT}/{DBNAME}"


def _render(text: str, conn) -> str:
    """把 SQL 文本里的 psql 变量 :'name' 替换为对应密码的 SQL 字面量。

    用 psycopg.sql.Literal(...).as_string(conn) 做安全转义，避免密码里的引号破坏 SQL；
    未知变量名直接报错，避免“密码为空”这类静默错误。
    """
    def repl(match):
        name = match.group(1)
        if name not in ROLE_PASSWORDS:
            raise KeyError(f"SQL 引用了未知变量 :'{name}'")
        return sql.Literal(ROLE_PASSWORDS[name]).as_string(conn)
    return _PSQL_VAR_RE.sub(repl, text)


def _apply_sql_file(path, conn) -> None:
    """执行单个 SQL 文件（含多条语句）。

    psycopg 在“不传参数”时用简单查询协议，允许一个字符串里包含多条语句；
    任一语句出错会抛异常（等价于 psql 的 ON_ERROR_STOP=1）。
    """
    conn.execute(_render(Path(path).read_text(encoding="utf-8"), conn))


@pytest.fixture(scope="session", autouse=True)
def _apply_schema():
    """会话级、自动执行：把 database/sql/*.sql 灌进临时库（只做一次）。

    · autouse + session：整套测试开始前建好 schema，全程复用；
    · autocommit=True：DDL/GRANT 直接生效，无需手工 commit；
    · sorted()：保证按 0001→0002→0003→0004 的文件名顺序执行；
    · 以超级用户连接：0001 需要 CREATE ROLE / SET ROLE role_pa_admin。
    """
    with psycopg.connect(dsn(PGUSER, PGPASSWORD), autocommit=True) as conn:
        for sql_file in sorted(SQL_DIR.glob("*.sql")):
            _apply_sql_file(sql_file, conn)
    yield


# 各角色连接串：测试用它们“以该角色身份”连库，验证真实权限
@pytest.fixture(scope="session")
def superuser_dsn() -> str:
    """超级用户（postgres）连接串 —— 用于查系统目录、造测试数据。"""
    return dsn(PGUSER, PGPASSWORD)


@pytest.fixture(scope="session")
def backend_dsn() -> str:
    """backend 应用角色（role_pa_backend）连接串。"""
    return dsn("role_pa_backend", ROLE_PASSWORDS["backend_pwd"])


@pytest.fixture(scope="session")
def ai_dsn() -> str:
    """ai-engine 应用角色（role_pa_ai）连接串。"""
    return dsn("role_pa_ai", ROLE_PASSWORDS["ai_pwd"])


@pytest.fixture
def superuser(superuser_dsn):
    """超级用户连接对象（函数级）。

    autocommit=True 很关键：负向测试里的语句会被拒绝（抛异常），
    若处于事务中会导致后续语句全部失败；autocommit 下每条语句独立，互不影响。
    """
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def apply_sql_file(superuser):
    """返回一个“应用 SQL 文件”的函数，供‘迁移可重放（幂等）’类测试使用。"""
    def _apply(path):
        _apply_sql_file(path, superuser)
    return _apply
