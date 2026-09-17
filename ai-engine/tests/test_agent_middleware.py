"""Agent 中间件单测（纯内存：直接驱动钩子，不需要 PG/Redis/真实 LLM）。

覆盖:
  · ModelCallLimitMiddleware：恰好放行 max_calls 次，之后置 stop_reason=model_call_limit；
  · ToolCallLimitMiddleware：计数放行；超限拦截并收敛（不调用内层 handler）；
  · ToolRetryMiddleware：抛错重试后有结果；重试耗尽 → ok=False 兜底（异常不外泄）；
    内层正常返回 ok=False 时**不重试**（避免放大确定性失败）；
  · 组合顺序：wrap_tool_call 洋葱由外到内（列表第一个最先看到调用、最后看到结果），
    before_model 按列表顺序执行；
  · after_model：可改写 response（能力留给后续扩展，如追加审计信息）。

说明:
    中间件是「成本治理 + 异常兜底」的落点（预算熔断对齐 README 的待办），
    因此这里对边界（max_calls 恰好放行）做精确断言。
"""

from __future__ import annotations

from typing import Callable

from src.ports import ToolCall, ToolResult
from src.workflowcore.agent.middleware import (
    AgentBudget,
    AgentContext,
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
    default_middlewares,
)


def _ctx(*, max_turns: int = 4, max_tool_calls: int = 8, turn: int = 1) -> AgentContext:
    """构造中间件上下文（预算 + 轮次）。

    参数:
        max_turns: 模型调用上限。
        max_tool_calls: 工具调用上限。
        turn: 当前轮次。
    返回:
        AgentContext 实例。
    """
    return AgentContext(
        thread_id="thread-1",
        turn=turn,
        budget=AgentBudget(max_turns=max_turns, max_tool_calls=max_tool_calls),
    )


def _call(name: str = "get_product_facts") -> ToolCall:
    """构造一次工具调用。

    参数:
        name: 工具名。
    返回:
        ToolCall 实例。
    """
    return ToolCall(id="c1", name=name, arguments={})


def test_model_call_limit_allows_exact_budget_then_stops():
    """模型上限：恰好放行 max_calls 次；第 max_calls+1 轮被取消并收敛。"""
    middleware = ModelCallLimitMiddleware(2)
    ctx = _ctx()

    for turn in (1, 2):
        ctx.turn = turn
        ctx.budget.turns_used = turn
        middleware.before_model(ctx)
        assert ctx.stop_reason == "", f"第 {turn} 轮不应被熔断"

    ctx.turn = 3
    ctx.budget.turns_used = 3
    middleware.before_model(ctx)
    assert ctx.stop_reason == "model_call_limit"


def test_tool_call_limit_counts_then_intercepts():
    """工具上限：max_calls 次内正常计数放行，之后拦截并置 stop_reason。"""
    middleware = ToolCallLimitMiddleware(2)
    ctx = _ctx()

    def _handler(call: ToolCall) -> ToolResult:
        """内层执行体：直接返回成功结果。

        参数:
            call: 本次工具调用。
        返回:
            成功 ToolResult。
        """
        return ToolResult(call_id=call.id, name=call.name, ok=True, content={"ok": 1})

    first = middleware.wrap_tool_call(_call(), ctx=ctx, handler=_handler)
    second = middleware.wrap_tool_call(_call(), ctx=ctx, handler=_handler)
    third = middleware.wrap_tool_call(_call(), ctx=ctx, handler=_handler)

    assert first.ok is True and second.ok is True
    assert ctx.budget.tool_calls_used == 2
    assert third.ok is False and third.error == "tool_call_limit_reached"
    assert ctx.stop_reason == "tool_call_limit"


def test_tool_retry_recovers_from_transient_error():
    """工具重试：首次抛错、第二次成功 → 返回成功结果且异常不外泄。"""
    calls = {"n": 0}

    def _handler(call: ToolCall) -> ToolResult:
        """内层执行体：首次抛错，之后成功。

        参数:
            call: 本次工具调用。
        返回:
            成功 ToolResult。
        异常:
            TimeoutError: 仅第一次调用抛出（模拟瞬时故障）。
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("db timeout")
        return ToolResult(call_id=call.id, name=call.name, ok=True, content={"rows": 2})

    result = ToolRetryMiddleware(2).wrap_tool_call(_call(), ctx=_ctx(), handler=_handler)

    assert calls["n"] == 2
    assert result.ok is True and result.content == {"rows": 2}


def test_tool_retry_falls_back_to_failed_result():
    """工具异常兜底：重试耗尽后返回 ok=False 结果（绝不把异常抛回图执行器）。"""
    attempts = {"n": 0}

    def _handler(call: ToolCall) -> ToolResult:
        """内层执行体：始终抛错。

        参数:
            call: 本次工具调用（未使用）。
        异常:
            RuntimeError: 始终抛出（模拟数据库不可用）。
        """
        attempts["n"] += 1
        raise RuntimeError("psycopg down")

    result = ToolRetryMiddleware(3).wrap_tool_call(_call(), ctx=_ctx(), handler=_handler)

    assert attempts["n"] == 3
    assert result.ok is False
    assert "psycopg down" in (result.error or "")


def test_tool_retry_does_not_retry_business_failure():
    """确定性失败（内层返回 ok=False）不重试：避免放大无谓调用。"""
    attempts = {"n": 0}

    def _handler(call: ToolCall) -> ToolResult:
        """内层执行体：返回业务失败结果（不抛异常）。

        参数:
            call: 本次工具调用。
        返回:
            ok=False 的 ToolResult。
        """
        attempts["n"] += 1
        return ToolResult(call_id=call.id, name=call.name, ok=False, content={}, error="unknown_tool")

    result = ToolRetryMiddleware(3).wrap_tool_call(_call(), ctx=_ctx(), handler=_handler)

    assert attempts["n"] == 1
    assert result.error == "unknown_tool"


class _OrderProbe(AgentMiddleware):
    """顺序探针：把「进入/离开」记到共享列表，用于断言洋葱嵌套顺序。"""

    def __init__(self, label: str, log: list[str]) -> None:
        """初始化。

        参数:
            label: 本探针标签（写入日志）。
            log: 共享日志列表（同一列表可跨探针观察顺序）。
        """
        self.label = label
        self.log = log

    def before_model(self, ctx: AgentContext) -> None:
        """记录 before_model 调用顺序。

        参数:
            ctx: 共享上下文（本探针不使用）。
        返回:
            无返回值。
        """
        self.log.append(f"before:{self.label}")

    def wrap_tool_call(
        self,
        call: ToolCall,
        *,
        ctx: AgentContext,
        handler: Callable[[ToolCall], ToolResult],
    ) -> ToolResult:
        """记录进入 / 离开顺序以验证嵌套层次。

        参数:
            call: 本次工具调用。
            ctx: 共享上下文（忽略）。
            handler: 内层执行体。
        返回:
            内层结果。
        """
        self.log.append(f"enter:{self.label}")
        result = handler(call)
        self.log.append(f"exit:{self.label}")
        return result


def test_middleware_composition_order_is_outside_in():
    """组合顺序：列表第一个是最外层（先 enter、最后 exit）；before_model 按列表顺序。"""
    log: list[str] = []
    outer = _OrderProbe("outer", log)
    inner = _OrderProbe("inner", log)

    def _handler(call: ToolCall) -> ToolResult:
        """最内层执行体：记录一次核心调用。

        参数:
            call: 本次工具调用。
        返回:
            成功 ToolResult。
        """
        log.append("core")
        return ToolResult(call_id=call.id, name=call.name, ok=True, content={})

    ctx = _ctx()
    handler: Callable[[ToolCall], ToolResult] = _handler
    for middleware in reversed([outer, inner]):
        previous = handler

        def _wrapped(call: ToolCall, *, _middleware=middleware, _next=previous) -> ToolResult:
            """把中间件包在内层执行体之外（复刻 AgentLoop 的洋葱组合方式）。

            参数:
                call: 本次工具调用。
                _middleware: 当前层中间件（默认参数绑定，避免闭包捕获错层）。
                _next: 内层执行体（默认参数绑定）。
            返回:
                中间件处理后的 ToolResult。
            """
            return _middleware.wrap_tool_call(call, ctx=ctx, handler=_next)

        handler = _wrapped

    handler(_call())
    outer.before_model(ctx)
    inner.before_model(ctx)

    assert log == [
        "enter:outer",
        "enter:inner",
        "core",
        "exit:inner",
        "exit:outer",
        "before:outer",
        "before:inner",
    ]


def test_default_middlewares_are_ordered_outside_in():
    """默认中间件链：模型上限（最外）→ 工具上限 → 重试兜底（最内，紧贴执行体）。"""
    names = [middleware.name for middleware in default_middlewares()]

    assert names == ["model_call_limit", "tool_call_limit", "tool_retry"]


def test_after_model_can_rewrite_response():
    """after_model：可改写返回（为后续「追加审计信息」类扩展预留能力）。"""

    class _Rewrite(AgentMiddleware):
        """把助手文本改写的探针中间件。"""

        def after_model(self, ctx: AgentContext, response: dict) -> dict:
            """改写助手文本。

            参数:
                ctx: 共享上下文（忽略）。
                response: 运行时返回的助手轮次。
            返回:
                改写后的 response（content 追加标记）。
            """
            return {**response, "content": f"{response.get('content') or ''}[rewritten]"}

    rewritten = _Rewrite().after_model(_ctx(), {"content": "hi", "tool_calls": []})

    assert rewritten["content"] == "hi[rewritten]"
