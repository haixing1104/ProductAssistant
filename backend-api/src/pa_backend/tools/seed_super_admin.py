#!/usr/bin/env python3
"""引导平台超管：创建「平台组织」+ 一个 ``is_superuser`` 的 admin 账号。

为什么要有这个脚本（而不是把账号写进 ``database/sql/0007_*.sql``）:
    口令必须是 bcrypt 哈希才能与登录校验（``core.security``）同源；把哈希**提交进 git**
    既等于把凭据写进版本历史，又让「轮换口令 = 改代码 + 提交」。本脚本从环境变量 /
    交互输入取明文，入库前哈希（明文不落库、不进日志、不写文件），因此:
      · ``infra/.env`` 已被 ``.gitignore`` 忽略（且历史上从未入库），凭据不进仓库；
      · 改了 ``BOOTSTRAP_ADMIN_PASSWORD`` 重跑 ``--reset-password`` 即可轮换。

口令来源优先级（**默认交互输入，最安全**）:
    ① ``--password-stdin``（从管道读一行，适合 CI/自动化）；
    ② 环境变量 ``BOOTSTRAP_ADMIN_PASSWORD``（无交互部署用；注意它会被同机进程 env 看到）；
    ③ ``getpass`` 交互输入（默认；不进 shell history、不进 env、不进 .env）。
    刻意**不提供** ``--password <明文>``：那会让口令出现在 ``ps`` 与 shell history 里。

幂等语义（重复执行安全）:
    · 组织：先按固定 UUID 找，再按名称找；都没有才新建 —— 因此可重复执行；
    · 账号：按 ``(org_id, username)`` 找（与 DB 唯一约束一致）；已存在时**默认跳过**
      （绝不覆盖已被人工轮换过的口令）；只有显式 ``--reset-password`` 才更新哈希。

运行前提:
    · 已执行 ``database/sql/0007_superuser.sql``（否则脚本会明确报错并给出命令）；
    · 环境里有 ``ROLE_PA_BACKEND_PWD`` 或 ``BACKEND_PG_DSN``（``role_pa_backend`` 对
      ``organizations`` / ``sys_users`` 有 INSERT/UPDATE，见 0002_roles_grants.sql）。

用法::

    cd backend-api
    set -a && . ../infra/.env && set +a          # 注入 PG 连接与角色口令
    PYTHONPATH=src python -m pa_backend.tools.seed_super_admin            # 交互输入口令
    PYTHONPATH=src python -m pa_backend.tools.seed_super_admin --reset-password

安全提醒（务必读）:
    · 该账号是**跨租户**的（``X-Org-Id`` 可切到任意组织，见 ``core/deps``），一旦泄露
      等于整套系统 + 全部租户数据被接管；请用 16+ 位随机口令，不要用手机号/工号这类可猜值；
    · 用户名不要叫 ``admin``：``sys_users`` 的用户名只在组织内唯一，跨组织同名需要
      ``org_name`` 消歧（``services/auth_service``），与业务租户的 admin 撞名会导致登录 400；
    · 忘记口令时唯一的恢复路径就是本脚本（``--reset-password``）——注册入口关闭后，
      这是**唯一**能进入系统的账号，请勿同时削弱它和注册开关的应急能力。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.db import create_engine_and_session
from ..core.security import hash_password
from ..models.orm import Organization, SysUser

__all__ = ["main", "seed_super_admin"]

#: 平台组织的固定 UUID：让「按 id 复用」成为幂等锚点（不依赖名称是否被人工改过）
PLATFORM_ORG_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
#: 超管账号的固定 UUID（仅用于「首次插入」；已存在时按 ``(org_id, username)`` 命中）
SUPER_ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a2")

#: 口令最小长度（与登录页/注册接口口径一致：少于 8 位在入口就被拒）
MIN_PASSWORD_LENGTH = 8

#: 退出码：0 成功；2 前置条件不满足（未迁移 / 未配置连接 / 口令不合规）
EXIT_OK = 0
EXIT_PRECONDITION = 2


def _env_str(key: str) -> str | None:
    """读环境变量（去空白；空串按未设置处理）。

    参数:
        key: 环境变量名（须在 ``infra/.env.template`` 登记）。
    返回:
        去空白后的值；未设置或为空串时返回 None。
    """
    value = (os.getenv(key) or "").strip()
    return value or None


def _read_password(*, username: str, use_stdin: bool) -> str | None:
    """按优先级取得明文口令（见模块 docstring；**绝不**回显或落盘）。

    参数:
        username: 用户名（仅用于交互提示文案）。
        use_stdin: True = 从 stdin 读一行（``--password-stdin``）。
    返回:
        明文口令；取不到或两次输入不一致时返回 None（由调用方退出）。
    """
    if use_stdin:
        return sys.stdin.readline().strip() or None
    from_env = _env_str("BOOTSTRAP_ADMIN_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        print("!! 非交互环境且未提供口令：请用 --password-stdin 或 BOOTSTRAP_ADMIN_PASSWORD", file=sys.stderr)
        return None
    first = getpass.getpass(f"请输入 {username} 的口令（不回显，至少 {MIN_PASSWORD_LENGTH} 位）: ")
    second = getpass.getpass("请再输入一次确认: ")
    if first != second:
        print("!! 两次输入不一致，已中止", file=sys.stderr)
        return None
    return first or None


async def _has_superuser_column(session: AsyncSession) -> bool:
    """检查 ``sys_users.is_superuser`` 是否已迁移（未迁移时给出可执行提示）。

    参数:
        session: DB 会话。
    返回:
        列存在返回 True。
    注意:
        用 information_schema 显式探测，而不是等 ORM 查询抛 ``UndefinedColumn``：
        后者要在事务中断后才能给出提示，且错误文案对运维不友好。
    """
    row = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'schema_pa_backend' AND table_name = 'sys_users' "
            "AND column_name = 'is_superuser'"
        )
    )
    return row.scalar_one_or_none() is not None


async def _ensure_org(session: AsyncSession, org_name: str) -> tuple[Organization, str]:
    """取得/创建平台组织（先固定 UUID、再名称，都没有才新建）。

    参数:
        session: DB 会话。
        org_name: 组织名称。
    返回:
        ``(组织, "reused" | "created")``。
    """
    by_id = await session.get(Organization, PLATFORM_ORG_ID)
    if by_id is not None:
        return by_id, "reused"
    existing = (
        await session.execute(select(Organization).where(Organization.name == org_name).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, "reused"
    org = Organization(id=PLATFORM_ORG_ID, name=org_name, status="active")
    session.add(org)
    await session.flush()
    return org, "created"


async def _ensure_user(
    session: AsyncSession, org: Organization, *, username: str, password: str, reset_password: bool
) -> tuple[SysUser, str]:
    """取得/创建超管账号（``role='admin'`` + ``is_superuser=True``）。

    参数:
        session: DB 会话。
        org: 目标组织（来自 ``_ensure_org``）。
        username: 登录名。
        password: 明文口令（本函数内 bcrypt，明文不落库/不进日志）。
        reset_password: True = 已存在时覆盖口令；False = 已存在即跳过（保护人工轮换过的口令）。
    返回:
        ``(账号, "created" | "skipped" | "password-reset")``。
    注意:
        命中判据用 ``(org_id, username)`` —— 与 ``uq_sys_users_org_username`` 一致；
        插入时优先用固定 UUID，若该 UUID 已被别的行占用则退化为随机 UUID（仍幂等：
        下一次执行会由 ``(org_id, username)`` 命中）。
    """
    user = (
        await session.execute(
            select(SysUser).where(SysUser.org_id == org.id, SysUser.username == username)
        )
    ).scalar_one_or_none()
    if user is not None:
        if not reset_password:
            return user, "skipped"
        user.hashed_password = hash_password(password)
        user.role = "admin"
        user.status = "active"
        user.is_superuser = True
        await session.flush()
        return user, "password-reset"

    taken = await session.get(SysUser, SUPER_ADMIN_ID)
    new_id = SUPER_ADMIN_ID if taken is None else uuid.uuid4()
    created = SysUser(
        id=new_id,
        org_id=org.id,
        username=username,
        hashed_password=hash_password(password),
        role="admin",
        status="active",
        is_superuser=True,
    )
    session.add(created)
    await session.flush()
    return created, "created"


async def seed_super_admin(
    *, org_name: str, username: str, password: str, reset_password: bool = False
) -> int:
    """执行引导（幂等）：建组织 + 建/更新超管账号，并打印结果摘要。

    参数:
        org_name: 平台组织名称（不存在则新建）。
        username: 超管登录名。
        password: 明文口令（仅存在于本进程内存，入库即哈希）。
        reset_password: 已存在时是否覆盖口令。
    返回:
        进程退出码（0 成功；2 前置条件不满足）。
    注意:
        摘要只打印 id/名称/角色与动作，**绝不打印口令或哈希**（与 ``auth_service``
        的「明文不落库、不进日志」同一口径）。
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 缺 ROLE_PA_BACKEND_PWD / BACKEND_PG_DSN：给出可照做的提示
        print(f"!! 无法建立数据库配置：{exc}", file=sys.stderr)
        print("   请先：set -a && . ../infra/.env && set +a", file=sys.stderr)
        return EXIT_PRECONDITION

    engine, session_factory = create_engine_and_session(settings)
    try:
        async with session_factory() as session:
            if not await _has_superuser_column(session):
                print(
                    "!! sys_users.is_superuser 不存在：请先执行 database/sql/0007_superuser.sql"
                    "（已初始化过的库需手工 psql 执行：pgsql-setup.sh 会拒绝重跑）",
                    file=sys.stderr,
                )
                return EXIT_PRECONDITION
            org, org_action = await _ensure_org(session, org_name)
            user, user_action = await _ensure_user(
                session,
                org,
                username=username,
                password=password,
                reset_password=reset_password,
            )
            await session.commit()
            org_id, user_id = str(org.id), str(user.id)
    finally:
        await engine.dispose()

    print("==> 引导完成")
    print(f"    组织: {org_name} ({org_id}) [{org_action}]")
    print(f"    账号: {username} ({user_id}) role=admin is_superuser=true [{user_action}]")
    if user_action == "skipped":
        print("    提示: 账号已存在，口令未改动（如需轮换请加 --reset-password）")
    print("    登录后在界面选择要操作的组织（走 X-Org-Id 头，见 core/deps）")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数 → 取口令 → 引导。

    参数:
        argv: 命令行参数（None = 取 ``sys.argv[1:]``，便于测试注入）。
    返回:
        进程退出码。
    """
    parser = argparse.ArgumentParser(
        description="引导平台超管（平台组织 + is_superuser admin）。口令默认交互输入，不回显。"
    )
    parser.add_argument(
        "--username",
        default=_env_str("BOOTSTRAP_ADMIN_USERNAME") or "superadmin",
        help="超管登录名（默认取 BOOTSTRAP_ADMIN_USERNAME，再默认 superadmin；勿用 admin，避免跨组织同名歧义）",
    )
    parser.add_argument(
        "--org-name",
        default=_env_str("BOOTSTRAP_ADMIN_ORG_NAME") or "平台管理",
        help="平台组织名称（默认取 BOOTSTRAP_ADMIN_ORG_NAME，再默认「平台管理」）",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从 stdin 读一行作为口令（CI/自动化；明文不进 env 与 history）",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="账号已存在时覆盖口令（口令轮换；默认跳过不覆盖）",
    )
    args = parser.parse_args(argv)

    password = _read_password(username=args.username, use_stdin=args.password_stdin)
    if not password:
        print("!! 未取得口令，已中止", file=sys.stderr)
        return EXIT_PRECONDITION
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"!! 口令至少 {MIN_PASSWORD_LENGTH} 位（与注册接口口径一致），已中止", file=sys.stderr)
        return EXIT_PRECONDITION

    return asyncio.run(
        seed_super_admin(
            org_name=args.org_name,
            username=args.username,
            password=password,
            reset_password=args.reset_password,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
