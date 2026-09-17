"""Agent 循环单测（纯内存：脚本化假运行时 + 假工具，不需要 PG/Redis/真实 LLM）。

覆盖:
  · 正常往返：模型请求工具 → 工具执行 → 结果回填 → 模型收敛（stop_reason=completed）；
  · 预算熔断：模型调用上限触发后**不再发起模型调用**（省 token），并以 stop_reason 收敛；
  · 工具上限：同轮多个工具调用中，超限的那次被拦下且循环立即收敛；
  · 未知工具：收敛为 ok=False 的结果回填给模型（不抛异常、不中断循环）；
  · 运行时异常：LLM/网络异常被收敛为 stop_reason=runtime_error，绝不抛出
    （研究属增强步骤，失败不得让整条生成链路 failed）；
  · 事件契约：agent.tool / agent.done 写入 evt:{thread_id}（未注入 EventBus 时零副作用）。

说明:
    本用例只验证「编排 + 中间件治理」语义，不触达任何外部依赖；
    真实取数（PG/Milvus）与真实 function calling 见 tests/test_agent_tools_pg.py
    与 tests/test_llm_function_calling.py。
"""

from __future__ import annotations

from typing import Any

from src.ports import AgentRuntime
from src.workflowcore.agent import AgentLoop, ToolRegistry, build_tool
from src.workflowcore.agent.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

THREAD_ID = "22222222-2222-4222-8222-222222222222"


class ScriptedRuntime(AgentRuntime):
    """脚本化假运行时：按顺序回放预设助手轮次，并记录每轮收到的 messages。"""

    def __init__(self, turns: list[dict]) -> None:
        """初始化。

        参数:
            turns: 预设轮次列表，每项形如
                {"content": str, "tool_calls": [{"id": ..., "name": ..., "arguments": {...}}]}。
        """
        self._turns = list(turns)
        self.calls: list[list[dict]] = []

    def run_turn(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        """回放下一轮（脚本用尽时返回「收敛」形态，避免测试挂死）。

        参数:
            messages: 内核传入的消息列表（记录用于断言回填内容）。
            tools: 工具声明（本假实现忽略，但保持端口签名一致）。
            tool_choice: 工具选择策略（忽略）。
        返回:
            {"content": ..., "tool_calls": [...]}。
        """
        self.calls.append([dict(message) for message in messages])
        if not self._turns:
            return {"content": "（脚本用尽）", "tool_calls": []}
        return self._turns.pop(0)


class ExplodingRuntime(AgentRuntime):
    """总是抛异常的假运行时（模拟 LLM 网络/协议故障）。"""

    def run_turn(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        """无条件抛错，用于验证循环的降级行为。

        参数:
            messages: 内核传入的消息列表（忽略）。
            tools: 工具声明（忽略）。
            tool_choice: 工具选择策略（忽略）。
        异常:
            RuntimeError: 始终抛出。
        """
        raise RuntimeError("boom")


class RecordingEventBus:
    """记录型假事件总线（内存列表，便于断言事件类型）。"""

    def __init__(self) -> None:
        """初始化空记录。"""
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        """记录一次发布。

        参数:
            topic: 目标 topic（期望 evt:{thread_id}）。
            payload: 事件载荷。
        返回:
            无返回值。
        """
        self.events.append((topic, payload))

    def types(self) -> list[str]:
        """返回已发布事件的 type 列表（顺序即发布顺序）。

        返回:
            事件类型字符串列表。
        """
        return [str(payload.get("type")) for _topic, payload in self.events]


def _echo_registry() -> ToolRegistry:
    """构造一个只回显参数的工具注册表（模拟只读取数工具）。"""

    def _echo(arguments: dict[str, Any]) -> dict:
        """回显模型给的参数。

        参数:
            arguments: 模型给的参数 dict。
        返回:
            {"echo": arguments}。
        """
        return {"echo": arguments}

    return ToolRegistry(
        [
            build_tool(
                name="echo",
                description="回显参数（测试用只读工具）",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
                handler=_echo,
            )
        ]
    )


def _loop(runtime: AgentRuntime, *, max_turns: int = 4, max_tool_calls: int = 8,
          middlewares: list[AgentMiddleware] | None = None, event_bus=None) -> AgentLoop:
    """装配带默认中间件的 AgentLoop（统一测试入口）。

    参数:
        runtime: 假运行时。
        max_turns: 模型调用上限。
        max_tool_calls: 工具调用上限。
        middlewares: 覆盖中间件链；None 用默认链。
        event_bus: 假事件总线；None 表示不发事件。
    返回:
        AgentLoop 实例。
    """
    return AgentLoop(
        runtime=runtime,
        registry=_echo_registry(),
        middlewares=middlewares,
        max_turns=max_turns,
        max_tool_calls=max_tool_calls,
        event_bus=event_bus,
    )


def test_agent_loop_calls_tool_then_converges():
    """正常往返：一轮工具调用后模型给出结论 → completed，且工具结果已回填。"""
    runtime = ScriptedRuntime(
        [
            {"content": "", "tool_calls": [{"id": "c1", "name": "echo", "arguments": {"q": "价格"}}]},
            {"content": "价格 19.9，库存充足", "tool_calls": []},
        ]
    )
    result = _loop(runtime).run(thread_id=THREAD_ID, question="核对商品事实")

    assert result.ok is True
    assert result.stop_reason == "completed"
    assert result.tool_calls_used == 1
    assert result.turns_used == 2
    assert "19.9" in result.context
    # 轨迹：第一轮 model（带 tool_calls）→ tool → 第二轮 model（收敛）
    assert [entry["event"] for entry in result.trace] == ["model", "tool", "model"]
    # 第二轮消息里应带上 tool 角色结果（回填 call_id + ok=True）
    tool_messages = [m for m in runtime.calls[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert '"ok": true' in tool_messages[0]["content"]


def test_agent_loop_stops_at_model_call_limit():
    """预算熔断：达到模型调用上限后不再发起模型调用，条件边直接收敛。"""
    turns = [
        {"content": "", "tool_calls": [{"id": f"c{i}", "name": "echo", "arguments": {"q": str(i)}}]}
        for i in range(5)
    ]
    runtime = ScriptedRuntime(turns)
    result = _loop(runtime, max_turns=2, max_tool_calls=99).run(thread_id=THREAD_ID, question="核对")

    assert result.stop_reason == "model_call_limit"
    assert result.ok is False
    assert result.turns_used == 2
    # 只发起了 2 次模型调用（预算用尽后条件边立即收敛，第 3 次不会发生）
    assert len(runtime.calls) == 2
    # 轨迹到第 2 轮 model 结束：预算已耗尽，未再执行这一轮请求的工具
    # （没有后续模型轮次来解读工具结果，执行只会白白消耗工具配额）
    assert [entry["event"] for entry in result.trace] == ["model", "tool", "model"]
    assert result.tool_calls_used == 1


def test_agent_loop_middleware_can_tighten_budget():
    """更紧的中间件策略：max_calls=1 时第 2 轮被「取消」（model_skipped，不调用 runtime）。"""
    runtime = ScriptedRuntime(
        [
            {"content": "", "tool_calls": [{"id": "c1", "name": "echo", "arguments": {"q": "a"}}]},
            {"content": "不应到达", "tool_calls": []},
        ]
    )
    middlewares = [ModelCallLimitMiddleware(1), ToolCallLimitMiddleware(9)]
    result = _loop(runtime, max_turns=4, middlewares=middlewares).run(thread_id=THREAD_ID, question="核对")

    assert result.stop_reason == "model_call_limit"
    assert result.turns_used == 1
    assert len(runtime.calls) == 1
    assert result.trace[-1]["event"] == "model_skipped"


def test_agent_loop_intercepts_tool_call_over_limit():
    """工具上限：同轮第 2 个调用被拦下，且循环立即收敛（不再发起模型调用）。"""
    runtime = ScriptedRuntime(
        [
            {
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "echo", "arguments": {"q": "a"}},
                    {"id": "c2", "name": "echo", "arguments": {"q": "b"}},
                ],
            },
            {"content": "不应到达", "tool_calls": []},
        ]
    )
    middlewares = [ModelCallLimitMiddleware(4), ToolCallLimitMiddleware(1)]
    result = _loop(runtime, middlewares=middlewares).run(thread_id=THREAD_ID, question="核对")

    assert result.tool_calls_used == 1
    assert result.stop_reason == "tool_call_limit"
    assert len(runtime.calls) == 1
    blocked = [entry for entry in result.trace if entry.get("event") == "tool" and not entry.get("ok")]
    assert blocked and blocked[0]["error"] == "tool_call_limit_reached"


def test_agent_loop_reports_unknown_tool_without_failing():
    """未知工具：收敛为失败结果回填（模型可换工具），循环不中断也不抛异常。"""
    runtime = ScriptedRuntime(
        [
            {"content": "", "tool_calls": [{"id": "x1", "name": "not_registered", "arguments": {}}]},
            {"content": "改用已有信息作答", "tool_calls": []},
        ]
    )
    result = _loop(runtime).run(thread_id=THREAD_ID, question="核对")

    assert result.ok is True
    unknown = [entry for entry in result.trace if entry.get("event") == "tool"]
    assert unknown and unknown[0]["ok"] is False and unknown[0]["error"] == "unknown_tool"
    tool_messages = [m for m in runtime.calls[1] if m.get("role") == "tool"]
    assert "unknown_tool" in tool_messages[0]["content"]


def test_agent_loop_degrades_on_runtime_error():
    """运行时异常：收敛为 runtime_error（不抛出），轨迹记录错误原因。"""
    result = _loop(ExplodingRuntime()).run(thread_id=THREAD_ID, question="核对")

    assert result.ok is False
    assert result.stop_reason == "runtime_error"
    assert "boom" in (result.error or "")
    assert result.trace[0]["event"] == "runtime_error"


def test_agent_loop_publishes_events_when_bus_injected():
    """事件契约：agent.tool / agent.done 发到 evt:{thread_id}。"""
    bus = RecordingEventBus()
    runtime = ScriptedRuntime(
        [
            {"content": "", "tool_calls": [{"id": "c1", "name": "echo", "arguments": {"q": "x"}}]},
            {"content": "结论", "tool_calls": []},
        ]
    )
    _loop(runtime, event_bus=bus).run(thread_id=THREAD_ID, question="核对")

    assert bus.types() == ["agent.tool", "agent.done"]
    assert all(topic == f"evt:{THREAD_ID}" for topic, _payload in bus.events)


def test_agent_loop_empty_registry_degrades_to_chat():
    """空工具表：退化为纯对话（无工具声明、无工具消息），循环仍能收敛。"""
    runtime = ScriptedRuntime([{"content": "无工具可用，直接作答", "tool_calls": []}])
    loop = AgentLoop(runtime=runtime, registry=ToolRegistry(), max_turns=2)
    result = loop.run(thread_id=THREAD_ID, question="核对")

    assert result.ok is True
    assert result.tool_calls_used == 0
    assert len(runtime.calls) == 1
    assert [m.get("role") for m in runtime.calls[0]] == ["system", "user"]


def test_tool_arguments_are_normalized_on_dirty_input():
    """脏参数防御：模型给出无法解析为 dict 的参数时包成 _raw，不抛异常。"""
    runtime = ScriptedRuntime(
        [
            {"content": "", "tool_calls": [{"id": "c1", "name": "echo", "arguments": "not-a-dict"}]},
            {"content": "ok", "tool_calls": []},
        ]
    )
    result = _loop(runtime).run(thread_id=THREAD_ID, question="核对")

    assert result.ok is True
    tool_messages = [m for m in runtime.calls[1] if m.get("role") == "tool"]
    assert '"_raw": "not-a-dict"' in tool_messages[0]["content"]
