"""Agent 默认开关与默认预算的回归单测（「默认开」是**行为默认值变更**，必须被钉住）。

覆盖:
  · `_agent_enabled_from_env()`：未配置 → **默认开**；0/false/no/空 → 关；1/true/YES/on → 开；
  · 关闭语义：`main()` **不构造** AgentRuntime（零成本短路，链路与未接入 Agent 时完全一致）；
  · 启用语义：`main()` 构造 AgentRuntime 并注入 `ListingWorker`，同时把收紧后的预算传下去；
  · 默认预算常量：`DEFAULT_MAX_TURNS=2` / `DEFAULT_MAX_TOOL_CALLS=3`。

为什么必须单独钉住默认值:
    改默认值**不会**让任何既有测试失败 —— `tests/` 不读该环境变量，全部显式注入 `agent_runtime`。
    也就是说「默认开 / 关」这件事在别处**没有任何回归保护**：谁把它改回 `"0"` 都不会被发现。

不依赖外部服务:
    测试完整执行 `main()`，但 `redis / signal / time.sleep / ListingWorker / AgentRuntime 工厂`
    全部打桩替换，不触碰真实网络与数据库（凭据未配置时各工厂本就返回 None）。
"""

from __future__ import annotations

import signal
import time

import pytest
import redis

import src.service.__main__ as service_main
from src.adapters import gateway_agent_runtime
from src.workflowcore.agent.middleware import DEFAULT_MAX_TOOL_CALLS, DEFAULT_MAX_TURNS


class FakeRedisClient:
    """最小 Redis 客户端替身：只记录 close() 是否被调用。"""

    def __init__(self) -> None:
        """初始化（closed 初值 False）。"""
        self.closed = False

    def close(self) -> None:
        """标记已关闭（main() 的 finally 会调用）。

        返回:
            无返回值。
        """
        self.closed = True


class FakeWorker:
    """ListingWorker 替身：收集构造入参，三个 consume_* 一律空转返回 None。"""

    #: 每次构造收集到的 kwargs（测试据此断言 agent_runtime / 预算是否被正确注入）
    instances: list[dict] = []

    def __init__(self, **kwargs) -> None:
        """记录构造入参。

        参数:
            **kwargs: main() 传入的全部端口与配置（agent_runtime / 预算等）。
        """
        self.kwargs = kwargs
        self.closed = False
        FakeWorker.instances.append(kwargs)

    def consume_generate_once(self, **_kwargs) -> None:
        """空转（模拟「本轮无消息」）。

        参数:
            **_kwargs: block_ms 等（本替身忽略）。
        返回:
            None。
        """
        return None

    def consume_approval_once(self, **_kwargs) -> None:
        """空转（模拟「本轮无消息」）。

        参数:
            **_kwargs: block_ms 等（本替身忽略）。
        返回:
            None。
        """
        return None

    def consume_product_purge_once(self, **_kwargs) -> None:
        """空转（模拟「本轮无消息」）。

        参数:
            **_kwargs: block_ms 等（本替身忽略）。
        返回:
            None。
        """
        return None

    def close(self) -> None:
        """标记已关闭（main() 的 finally 会调用）。

        返回:
            无返回值。
        """
        self.closed = True


def _run_main_with_stubs(monkeypatch) -> list[dict]:
    """打桩执行一次 `main()`（跑完一轮消费循环后由「假 sleep」触发优雅退出）。

    参数:
        monkeypatch: pytest 的 monkeypatch 夹具（测试结束自动还原全部打桩）。
    返回:
        FakeWorker 收集到的构造入参列表（长度恒为 1）。
    """
    FakeWorker.instances = []
    handlers: dict[int, object] = {}

    def _fake_signal(signum, handler):
        """替换 signal.signal：只记录处理器，不真正注册。

        参数:
            signum: 信号编号。
            handler: main() 传入的处理函数。
        返回:
            无返回值。
        """
        handlers[signum] = handler

    def _fake_sleep(_seconds: float) -> None:
        """替换 time.sleep：首轮即触发 SIGINT，让 while running 循环退出。

        参数:
            _seconds: 休眠时长（本替身忽略）。
        返回:
            无返回值。
        """
        handler = handlers.get(signal.SIGINT)
        if handler is not None:
            handler(signal.SIGINT, None)

    # 去凭据：本用例只验证开关接线，不依赖（也不该受）本机环境变量影响。
    # 否则 build_image_cogview_gateway_from_env() 可能因「有 Key 但缺模型/基址」抛 ValueError。
    for name in ("LLM_PROVIDER", "ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_IMAGE_MODEL", "MILVUS_URI"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("AI_ENGINE_PG_DSN", "postgresql://role_pa_ai:pwd@localhost:5432/db")
    monkeypatch.setenv("AI_ENGINE_SETUP_PG_DSN", "postgresql://role_pa_ai_setup:pwd@localhost:5432/db")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: FakeRedisClient())
    monkeypatch.setattr(signal, "signal", _fake_signal)
    monkeypatch.setattr(time, "sleep", _fake_sleep)
    monkeypatch.setattr(service_main, "ListingWorker", FakeWorker)

    service_main.main()
    return FakeWorker.instances


def test_agent_enabled_defaults_to_true_when_env_unset(monkeypatch) -> None:
    """未配置 AI_ENGINE_AGENT_ENABLED 时 Agent **默认开启**（本轮的行为变更点）。"""
    monkeypatch.delenv("AI_ENGINE_AGENT_ENABLED", raising=False)
    assert service_main._agent_enabled_from_env() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", " No "])
def test_agent_enabled_explicit_off(monkeypatch, raw: str) -> None:
    """显式关闭值（大小写与两侧空白容错）一律收敛为关。"""
    monkeypatch.setenv("AI_ENGINE_AGENT_ENABLED", raw)
    assert service_main._agent_enabled_from_env() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "YES", "on", " On "])
def test_agent_enabled_explicit_on_tolerates_case(monkeypatch, raw: str) -> None:
    """显式开启值大小写与空白容错（环境变量手写容易带空格）。"""
    monkeypatch.setenv("AI_ENGINE_AGENT_ENABLED", raw)
    assert service_main._agent_enabled_from_env() is True


@pytest.mark.parametrize("raw", ["", "   "])
def test_agent_enabled_blank_falls_back_to_default_on(monkeypatch, raw: str) -> None:
    """空串 / 纯空白视为「未配置」→ 用默认（**开**）。

    与仓库其它「默认开」开关同口径（adapters/image_pillow._truthy）：`VAR=` 这种被清空的写法
    不应把默认打开的能力静默关掉。
    """
    monkeypatch.setenv("AI_ENGINE_AGENT_ENABLED", raw)
    assert service_main._agent_enabled_from_env() is True


def test_default_budget_is_tightened() -> None:
    """默认预算必须被收紧到 2 轮 / 3 次（默认开之后用它兜住成本增量）。

    注意:
        再往下调到 1 轮会让 tests/test_agent_tools.py 的节点接线用例（1 次工具 + 2 轮模型）
        取证不足 —— 故本断言同时是「不许继续下调」的护栏。
    """
    assert DEFAULT_MAX_TURNS == 2
    assert DEFAULT_MAX_TOOL_CALLS == 3


def test_main_does_not_build_runtime_when_agent_disabled(monkeypatch) -> None:
    """关闭时**零成本短路**：不调用 build_agent_runtime_from_env，且 worker 收到 None。"""
    monkeypatch.setenv("AI_ENGINE_AGENT_ENABLED", "0")
    calls: list[int] = []

    def _spy():
        """替身工厂：记录被调用次数（关闭时本不应被调用）。

        返回:
            object 哨兵。
        """
        calls.append(1)
        return object()

    monkeypatch.setattr(gateway_agent_runtime, "build_agent_runtime_from_env", _spy)
    instances = _run_main_with_stubs(monkeypatch)

    assert calls == [], "关闭状态下不得构造 AgentRuntime（否则仍会产生额外模型调用）"
    assert instances[0]["agent_runtime"] is None


def test_main_builds_and_wires_runtime_when_agent_enabled(monkeypatch) -> None:
    """默认（不设变量）即启用：构造 runtime 并注入 worker，同时下发收紧后的预算。"""
    monkeypatch.delenv("AI_ENGINE_AGENT_ENABLED", raising=False)
    sentinel = object()

    def _factory():
        """替身工厂：返回固定哨兵，供断言「同一对象被注入 worker」。

        返回:
            object 哨兵。
        """
        return sentinel

    monkeypatch.setattr(gateway_agent_runtime, "build_agent_runtime_from_env", _factory)
    instances = _run_main_with_stubs(monkeypatch)

    kwargs = instances[0]
    assert kwargs["agent_runtime"] is sentinel
    assert kwargs["agent_max_turns"] == DEFAULT_MAX_TURNS
    assert kwargs["agent_max_tool_calls"] == DEFAULT_MAX_TOOL_CALLS


def test_main_reads_budget_from_env(monkeypatch) -> None:
    """预算可被环境变量显式放大（默认收紧，但复杂商品允许运维调高）。"""
    monkeypatch.delenv("AI_ENGINE_AGENT_ENABLED", raising=False)
    monkeypatch.setenv("AI_ENGINE_AGENT_MAX_TURNS", "4")
    monkeypatch.setenv("AI_ENGINE_AGENT_MAX_TOOL_CALLS", "8")
    monkeypatch.setattr(gateway_agent_runtime, "build_agent_runtime_from_env", lambda: object())
    instances = _run_main_with_stubs(monkeypatch)

    assert instances[0]["agent_max_turns"] == 4
    assert instances[0]["agent_max_tool_calls"] == 8
