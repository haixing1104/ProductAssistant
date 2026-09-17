"""登录失败限流用例（Redis 计数，``(username, ip)`` 维度）。

覆盖:
    · 窗口内累计失败达阈值 → 验密之前直接 429（连 bcrypt 都不做，防 CPU 放大）；
    · **成功即清零**：正常用户忘密码几次后输对，不应继续被历史失败拖累；
    · 计数按 ``(username, ip)`` 组合：别人的失败不影响本账号（避免同办公室一起被锁）。

⚠️ 夹具顺序陷阱（本文件踩过一次，务必按此写法）:
    ``settings`` 夹具在**构造时**读环境变量。若在**测试函数体**里 ``monkeypatch.setenv``，
    那时 ``client``/``settings`` 早已实例化 —— 改的是「下一份 Settings」而不是这一份，
    表现为「限流阈值明明改了却始终不触发」。所以阈值必须由**夹具**设置，
    并声明在 ``client`` 之前（pytest 按签名顺序实例化同作用域夹具）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import SeededOrg

LOGIN_URL = "/api/v1/auth/login"


@pytest.fixture
def two_failures_then_lock(monkeypatch) -> None:
    """把限流阈值压到「2 次失败即锁」并缩短窗口/锁定时长（必须在 settings 构造前生效）。"""
    monkeypatch.setenv("BACKEND_LOGIN_MAX_FAILURES", "2")
    monkeypatch.setenv("BACKEND_LOGIN_FAILURE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("BACKEND_LOGIN_LOCKOUT_SECONDS", "60")


async def test_account_locks_after_configured_failures(
    two_failures_then_lock, client: AsyncClient, seeded_org: SeededOrg
):
    """连续失败达阈值（2 次）后：第 3 次直接 429，并给出 ``Retry-After``。"""
    bad = {"username": seeded_org.username, "password": "wrong-password"}

    assert (await client.post(LOGIN_URL, json=bad)).status_code == 401
    assert (await client.post(LOGIN_URL, json=bad)).status_code == 401

    locked = await client.post(LOGIN_URL, json=bad)
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1

    # 锁定期内即使口令正确也进不来（否则限流形同虚设）
    good = {"username": seeded_org.username, "password": seeded_org.password}
    assert (await client.post(LOGIN_URL, json=good)).status_code == 429


async def test_success_clears_failure_counter(
    two_failures_then_lock, client: AsyncClient, seeded_org: SeededOrg
):
    """成功登录后失败计数清零：之后的单次失败不会立刻触发锁定。"""
    assert (
        await client.post(LOGIN_URL, json={"username": seeded_org.username, "password": "wrong-1"})
    ).status_code == 401
    assert (
        await client.post(
            LOGIN_URL, json={"username": seeded_org.username, "password": seeded_org.password}
        )
    ).status_code == 200

    # 计数已清零 → 再失败一次只是 401，不应 429
    again = await client.post(LOGIN_URL, json={"username": seeded_org.username, "password": "wrong-2"})
    assert again.status_code == 401


async def test_lock_is_scoped_per_username_and_ip(
    two_failures_then_lock, client: AsyncClient, seeded_org: SeededOrg
):
    """锁定只作用于（用户名 + IP）：另一个用户名不受影响。

    阈值语义（容易记错，故写死在用例里）:
        锁定在**达到阈值的那次**写入，但该次响应仍是 401（口令确实错了）；
        从**下一次**尝试起才是 429。
    """
    bad = {"username": seeded_org.username, "password": "nope"}
    assert (await client.post(LOGIN_URL, json=bad)).status_code == 401  # 第 1 次失败（计数 1）
    assert (await client.post(LOGIN_URL, json=bad)).status_code == 401  # 第 2 次失败（计数 2 → 写入锁定）
    assert (await client.post(LOGIN_URL, json=bad)).status_code == 429  # 锁定生效

    other = await client.post(LOGIN_URL, json={"username": "other-account", "password": "nope"})
    assert other.status_code == 401  # 未被牵连（仍是「用户名或密码错误」）
