"""数据库层：async engine 与 session factory。

口径:
    · 驱动 ``postgresql+psycopg``（psycopg3）——与 ``database/sql`` 的 DSN 只差驱动标记，
      派生逻辑见 ``core/config.py``；
    · ``pool_pre_ping=True``：连接被中间件/数据库重启掐断后自动重建，
      否则第一个请求会拿到死连接报错（本地开发重启 PG 时必踩）；
    · ``expire_on_commit=False``：commit 后仍可读对象属性，避免路由层「提交后再取值」触发隐式 IO。

红线:
    本模块**不建表、不迁移**。表结构归 ``database/sql/*.sql``（由 ``infra/scripts/pgsql-setup.sh`` 执行）；
    backend 只做 DML，且只以 ``role_pa_backend`` 连接（权限矩阵见 ``database/sql/0002_roles_grants.sql``）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings

__all__ = ["create_engine_and_session", "build_session_factory"]


def create_engine_and_session(settings: Settings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """创建 engine 与 session factory（应用启动时调用一次，停机时 ``engine.dispose()``）。

    参数:
        settings: 应用配置（读 ``database_url``）。
    返回:
        ``(engine, session_factory)`` 二元组；两者都挂在 ``app.state`` 上供依赖注入使用。
    """
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return engine, build_session_factory(engine)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """由 engine 构造 session factory（测试可直接注入自己的 engine）。

    参数:
        engine: 已创建的 async engine。
    返回:
        ``async_sessionmaker``（``expire_on_commit=False``，见模块 docstring）。
    """
    return async_sessionmaker(engine, expire_on_commit=False)
