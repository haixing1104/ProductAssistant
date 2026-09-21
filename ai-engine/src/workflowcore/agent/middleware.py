"""agent 中间件：模型轮次与工具调用之间的治理钩子（纯函数链，零第三方依赖）。

为什么自己实现一层「中间件」（而不是引入框架）:
    本仓需要的三件事都是「包住一次模型调用 / 一次工具调用」的装饰器语义：
      · 模型调用次数上限（预算熔断），
      · 工具调用次数上限（防止 Agent 反复取数把 token 与 DB 连接耗光），
      · 工具异常兜底 + 只读工具重试（工具失败绝不打断生成主链路）。
    `list[hook] + for 循环` 即可表达，无需引入 LangChain 的中间件体系
    （可保持 ai-engine 「真实 LLM 走智谱标准库 adapter、无需额外 SDK」的既有取向）。

钩子语义:
    before_model(ctx)                     —— 模型调用前；可写 ctx.stop_reason 以**取消**本次调用并收敛；
    after_model(ctx, response)            —— 模型调用后；返回新的 response（可改写 content / 追加元信息）；
    wrap_tool_call(call, *, ctx, handler) —— 工具调用洋葱模型：handler 是内层（最内层最终落到 ToolRegistry.run）。

组合顺序（重要，测试据此断言）:
    middlewares 列表 顺序 = 由外到内：
      · before_model / after_model 按列表顺序依次执行；
      · wrap_tool_call 按列表**逆序**嵌套，即列表第一个是最外层（最先看到调用、最后看到结果）。

终止语义（为什么用 stop_reason 而不是抛异常）:
    超限不是错误，而是「预算到顶、本线程收敛」——写成状态字段后，
    Agent 循环的条件边可直接据此走向 END，无需异常穿透图执行器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ...ports import ToolCall, ToolResult

__all__ = [
    "AgentBudget",
    "AgentContext",
    "AgentMiddleware",
    "ModelCallLimitMiddleware",
    "ToolCallLimitMiddleware",
    "ToolRetryMiddleware",
    "default_middlewares",
]

#: 默认模型调用上限（轮次）：一次商品生成最多 2 轮「模型 + 工具」往返。
#: 为什么是 2（而不是 4）：Agent 默认开启（AI_ENGINE_AGENT_ENABLED 默认 1），默认预算必须够紧，
#: 才能让「所有部署」的成本增量可控；需要更多轮取数的部署请显式调高（AI_ENGINE_AGENT_MAX_TURNS）。
#: ⚠️ 不要低于 2：tests/test_agent_tools.py 的节点接线用例是「1 次工具 + 2 轮模型」，
#:    收敛到 1 轮会取证不足（该用例恰好卡在本默认值边界上）。
DEFAULT_MAX_TURNS = 2

#: 默认工具调用上限：全部工具都是只读取数，超过该次数基本可判定为模型在打转。
#: 3 次 ≈ 够取「商品事实 + 一条历史证据 + 一次合规自查」；调高请用 AI_ENGINE_AGENT_MAX_TOOL_CALLS。
DEFAULT_MAX_TOOL_CALLS = 3


@dataclass
class AgentBudget:
    """一次 Agent 运行的成本预算与用量计数（模型轮次 / 工具调用次数）。

    注意:
        计数由 Agent 循环统一维护（loop 是唯一写入 turns_used 的地方），
        中间件只**读**计数做策略判断——避免多处写入同一个计数导致口径漂移。
    """

    max_turns: int = DEFAULT_MAX_TURNS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    turns_used: int = 0
    tool_calls_used: int = 0

    def tool_calls_remaining(self) -> int:
        """剩余可用工具调用次数（下限 0）。

        返回:
            max(0, max_tool_calls - tool_calls_used)。
        """
        return max(0, self.max_tool_calls - self.tool_calls_used)


@dataclass
class AgentContext:
    """中间件共享上下文：预算 + 终止原因 + 本轮消息与工具名。

    注意:
        stop_reason 非空即表示「本线程应收敛」，Agent 循环的条件边会据此走向 END；
        取值集合见 loop.AgentRunResult.stop_reason（completed / model_call_limit /
        tool_call_limit / runtime_error / max_turns）。
    """

    thread_id: str
    turn: int
    budget: AgentBudget
    stop_reason: str = ""
    messages: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)


class AgentMiddleware:
    """中间件基类：三个钩子全部可选（未覆盖即直通，不产生任何副作用）。"""

    #: 中间件名（用于 trace / 排障；实现方应覆盖为可读短名）
    name = "middleware"

    def before_model(self, ctx: AgentContext) -> None:
        """模型调用前钩子（默认直通）。

        参数:
            ctx: 本轮共享上下文；可写 ctx.stop_reason 取消本次模型调用。
        返回:
            无返回值（就地修改 ctx）。
        """

    def after_model(self, ctx: AgentContext, response: dict) -> dict:
        """模型调用后钩子（默认直通）。

        参数:
            ctx: 本轮共享上下文。
            response: 运行时返回的助手轮次（含 content / tool_calls）。
        返回:
            处理后的 response（默认原样返回）。
        """
        return response

    def wrap_tool_call(
        self,
        call: ToolCall,
        *,
        ctx: AgentContext,
        handler: Callable[[ToolCall], ToolResult],
    ) -> ToolResult:
        """工具调用洋葱钩子（默认直通内层）。

        参数:
            call: 本次工具调用。
            ctx: 共享上下文（工具钩子不参与模型轮次计数，turn 为当前轮次快照）。
            handler: 内层执行体（更内层最终落到 ToolRegistry.run）。
        返回:
            ToolResult；实现方既可以调用 handler，也可以直接返回自己的结果（如超限拦截）。
        """
        return handler(call)


class ModelCallLimitMiddleware(AgentMiddleware):
    """模型调用次数上限：达到上限即置 stop_reason=model_call_limit，本线程收敛。

    语义:
        这是「预算熔断」的落地点：超限时**不再发起模型调用**（省 token），
        由 Agent 回收已获得的工具证据并结束，而不是把整条生成链路判失败。
    """

    name = "model_call_limit"

    def __init__(self, max_calls: int = DEFAULT_MAX_TURNS) -> None:
        """初始化。

        参数:
            max_calls: 允许的最大模型调用次数（< 1 会被夹到 1，保证至少能跑一轮）。
        """
        self.max_calls = max(1, int(max_calls))

    def before_model(self, ctx: AgentContext) -> None:
        """用量已达上限则取消本次模型调用并收敛。

        参数:
            ctx: 本轮共享上下文（读 budget.turns_used）。
        返回:
            无返回值；超限时就地写 ctx.stop_reason。
        """
        if ctx.budget.turns_used > self.max_calls:
            ctx.stop_reason = "model_call_limit"


class ToolCallLimitMiddleware(AgentMiddleware):
    """工具调用次数上限：超限的工具调用被拦下（返回失败结果并收敛）。

    语义:
        拦截而不是抛异常：模型仍会收到一条「已达上限」的失败结果，
        但 ctx.stop_reason 被置位，条件边在本轮结束后立即收敛，不再发起新的模型调用。
    """

    name = "tool_call_limit"

    def __init__(self, max_calls: int = DEFAULT_MAX_TOOL_CALLS) -> None:
        """初始化。

        参数:
            max_calls: 允许的最大工具调用次数（< 1 会被夹到 1）。
        """
        self.max_calls = max(1, int(max_calls))

    def wrap_tool_call(
        self,
        call: ToolCall,
        *,
        ctx: AgentContext,
        handler: Callable[[ToolCall], ToolResult],
    ) -> ToolResult:
        """未超限则计数并放行；超限则拦截并置 stop_reason。

        参数:
            call: 本次工具调用。
            ctx: 共享上下文（读写 budget.tool_calls_used）。
            handler: 内层执行体。
        返回:
            正常放行时返回内层结果；超限时返回 ok=False 的拦截结果（含可读原因）。
        """
        if ctx.budget.tool_calls_used >= self.max_calls:
            ctx.stop_reason = "tool_call_limit"
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                content={"hint": "工具调用次数已达上限，请基于已有信息作答"},
                error="tool_call_limit_reached",
            )
        ctx.budget.tool_calls_used += 1
        return handler(call)


class ToolRetryMiddleware(AgentMiddleware):
    """工具重试 + 异常兜底：工具抛错时重试有限次，仍失败则转成失败结果。

    为什么可以放心重试:
        Agent 注册的工具全部是只读取数（无写入、无副作用），因此重试不会造成
        重复落库或状态错乱；写工具在本仓一律不允许注册（见 ports/tool.py 红线）。

    语义:
        · 成功返回（含内层主动返回的 ok=False）不重试，直接透传；
        · 抛异常则重试，最多 self.max_attempts 次；
        · 最终仍失败 → 返回 ok=False 的 ToolResult（**兜底**：绝不把异常抛回图执行器）。
    """

    name = "tool_retry"

    def __init__(self, max_attempts: int = 2) -> None:
        """初始化。

        参数:
            max_attempts: 单次工具调用的最大尝试次数（含首次；< 1 夹到 1）。
        """
        self.max_attempts = max(1, int(max_attempts))

    def wrap_tool_call(
        self,
        call: ToolCall,
        *,
        ctx: AgentContext,
        handler: Callable[[ToolCall], ToolResult],
    ) -> ToolResult:
        """带重试地执行内层工具调用，并把最终异常兜底为失败结果。

        参数:
            call: 本次工具调用。
            ctx: 共享上下文（只读；用于失败结果里带上线程信息）。
            handler: 内层执行体。
        返回:
            内层结果；或重试耗尽后的 ok=False 结果（error 携带异常摘要）。
        """
        last_error: Exception | None = None
        for _attempt in range(self.max_attempts):
            try:
                return handler(call)
            except Exception as exc:  # noqa: BLE001 兜底：工具异常绝不打断生成主链路
                last_error = exc
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=False,
            content={"thread_id": ctx.thread_id},
            error=f"{type(last_error).__name__}: {last_error}",
        )


def default_middlewares(
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    tool_attempts: int = 2,
) -> list[AgentMiddleware]:
    """产出默认中间件链（顺序 = 由外到内，见模块 docstring）。

    参数:
        max_turns: 模型调用次数上限（预算熔断阈值）。
        max_tool_calls: 工具调用次数上限。
        tool_attempts: 单次工具调用的最大尝试次数（只读工具重试）。
    返回:
        [ModelCallLimitMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware]；
        ToolRetryMiddleware 位于最内层（紧贴工具执行体），因此它能捕获工具抛出的
        原始异常并做重试与兜底。
    """
    return [
        ModelCallLimitMiddleware(max_turns),
        ToolCallLimitMiddleware(max_tool_calls),
        ToolRetryMiddleware(tool_attempts),
    ]
