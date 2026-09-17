"""loop：Agent 循环（LangGraph 两节点子图：agent ↔ tools，条件边控制收敛）。

拓扑:
    START → agent ─┬─(模型返回 tool_calls 且未到上限)─→ tools → agent …（循环）
                   └─(无工具调用 / 触发预算 / 运行时异常)─→ END

为什么用 LangGraph 子图（而不是 while 循环）:
    本仓内核层已是 LangGraph 编排（workflowcore/graph），复用同一套「节点 + 条件边 +
    状态增量合并」语义可让 Agent 循环与主图同构、可读且可测；子图**不挂 checkpointer**，
    避免把内层往返状态写进生成线程的 checkpoint（HITL 恢复语义因此完全不受影响）。
    子图状态只用普通 dict 消息（messages 为 OpenAI 兼容 dict 列表，经 operator.add 追加），
    **不引入任何 LangChain 消息对象**，保持 ai-engine 的依赖取向不变。

预算与中间件:
    · 预算（AgentBudget）由本模块统一计数：turns_used 在每轮模型调用前自增；
    · 策略（超限收敛 / 工具重试 / 工具异常兜底）全部在 agent/middleware.py，
      本模块只负责按约定顺序调用钩子（见 middleware.py 模块 docstring 的顺序说明）。

降级策略（重要）:
    · 工具异常：由 ToolRetryMiddleware 兜底为失败结果，循环继续（模型可换工具）；
    · 运行时（LLM/网络）异常：捕获后置 stop_reason=runtime_error 并收敛，
      Agent 不再产研究结论，但**不影响**主链路继续 generate/evaluate
      —— 研究阶段是「增强」而非「必需」，失败不该让整条商品生成判 failed；
    · 事件发布（evt:{thread_id}）失败按 ports/event_bus.py 契约上抛，不静默吞。
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ...ports import AgentRuntime, EventBus, ToolCall, ToolResult, normalize_tool_calls
from .middleware import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    AgentBudget,
    AgentContext,
    AgentMiddleware,
    default_middlewares,
)
from .registry import ToolRegistry

__all__ = ["AgentLoop", "AgentRunResult", "DEFAULT_SYSTEM_PROMPT"]

#: 默认系统提示：只读取数 + 不编造 + 结论简洁（决定模型何时调用工具）
DEFAULT_SYSTEM_PROMPT = (
    "你是电商上架文案的事实核对助手。你可以调用只读工具查询本商品的真实素材、"
    "历史文案版本、历史评估与人工审批记录，用于给文案撰写提供事实依据。"
    "规则：只依据工具返回的数据陈述事实，不得编造价格/库存/资质；"
    "工具返回失败时改用其它工具或直接说明缺失；最后用不超过 200 字给出 3-6 条事实要点。"
)


class _AgentState(TypedDict, total=False):
    """子图状态（普通 dict 消息，Redis/checkpoint 友好的纯 JSON 结构）。

    注意:
        messages 与 trace 使用 operator.add 追加式合并；其余字段为「最后写入生效」，
        因此节点只返回自己确实改变的字段（LangGraph 按字段做增量合并）。
    """

    messages: Annotated[list[dict], operator.add]
    trace: Annotated[list[dict], operator.add]
    turns: int
    tool_calls_used: int
    stop_reason: str
    context: str


@dataclass
class AgentRunResult:
    """一次 Agent 运行的收敛结果（供图节点写入 ListingState）。

    注意:
        ok=True 仅代表「模型自然收敛（completed）」；预算到顶或运行时异常时为 False，
        此时 context 可能是空串，但 trace 仍完整记录了过程，便于审计与排障。
    """

    ok: bool
    context: str = ""
    trace: list[dict] = field(default_factory=list)
    tool_calls_used: int = 0
    turns_used: int = 0
    stop_reason: str = ""
    error: str | None = None


class AgentLoop:
    """Agent 循环：装配「运行时 + 工具表 + 中间件链」，跑一轮研究并返回结论与轨迹。

    注意:
        · 一个实例对应一次运行（预算计数是实例状态），由调用方（node_agent）每次任务新建；
        · runtime=None 的场景由节点层短路，不要构造本类。
    """

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        registry: ToolRegistry,
        middlewares: list[AgentMiddleware] | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        event_bus: EventBus | None = None,
    ) -> None:
        """初始化。

        参数:
            runtime: Agent 运行时（真实实现 = 智谱 tools 协议；测试 = 脚本化假实现）。
            registry: 工具注册表（本次线程允许调用的只读工具）。
            middlewares: 中间件链（顺序 = 由外到内）；None 时使用 default_middlewares()。
            max_turns: 模型调用上限（预算熔断阈值，< 1 夹到 1）。
            max_tool_calls: 工具调用上限（< 1 夹到 1）。
            event_bus: 事件端口；None 表示不发过程事件（本地/单测场景）。
        """
        self.runtime = runtime
        self.registry = registry
        self.middlewares: list[AgentMiddleware] = (
            list(middlewares) if middlewares is not None else default_middlewares(max_turns=max_turns, max_tool_calls=max_tool_calls)
        )
        self.max_turns = max(1, int(max_turns))
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.event_bus = event_bus
        self._budget = AgentBudget(max_turns=self.max_turns, max_tool_calls=self.max_tool_calls)
        self._thread_id = ""
        # 实际发起的模型调用次数（与「进入的轮次序号」区分：被预算拦下的一轮不算调用）
        self._model_calls = 0
        self._error: str | None = None
        self._graph = self._build_graph()
    def run(self, *, thread_id: str, question: str, system_prompt: str | None = None) -> AgentRunResult:
        """运行一次 Agent 研究循环。

        参数:
            thread_id: 线程 ID（用于事件 topic `evt:{thread_id}` 与审计）。
            question: 交给 Agent 研究的问题（由 node_agent 依据商品素材拼装）。
            system_prompt: 覆盖默认系统提示；None 时用 DEFAULT_SYSTEM_PROMPT。
        返回:
            AgentRunResult；ok 仅当模型自然收敛（stop_reason=completed）。
        注意:
            · 运行时异常不抛出，而是收敛为 stop_reason=runtime_error（研究属增强步骤）；
            · 事件发布失败按端口契约上抛（不静默吞）。
        """
        self._thread_id = thread_id
        self._error = None
        self._model_calls = 0
        self._budget = AgentBudget(max_turns=self.max_turns, max_tool_calls=self.max_tool_calls)
        initial: dict = {
            "messages": [
                {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            "trace": [],
            "turns": 0,
            "tool_calls_used": 0,
            "stop_reason": "",
            "context": "",
        }
        final = self._graph.invoke(initial, config={"recursion_limit": self.max_turns * 2 + 2})
        stop_reason = str(final.get("stop_reason") or "")
        if not stop_reason and int(final.get("turns") or 0) >= self.max_turns:
            # 预算收敛有两条路径：中间件在「下一轮开始前」取消（已置位 stop_reason），
            # 或条件边在预算用尽时直接结束（此刻 stop_reason 为空）——后者在此归一为
            # model_call_limit，保证调用方拿到的终止原因始终可判读。
            stop_reason = "model_call_limit"
        result = AgentRunResult(
            ok=stop_reason == "completed",
            context=str(final.get("context") or ""),
            trace=[dict(item) for item in (final.get("trace") or [])],
            tool_calls_used=int(final.get("tool_calls_used") or 0),
            turns_used=self._model_calls,
            stop_reason=stop_reason,
            error=self._error,
        )
        self._publish(
            {
                "type": "agent.done",
                "data": {
                    "thread_id": thread_id,
                    "stop_reason": result.stop_reason,
                    "turns": result.turns_used,
                    "tool_calls": result.tool_calls_used,
                },
            }
        )
        return result

    def _build_graph(self):
        """构建并编译两节点子图（agent ↔ tools；不挂 checkpointer）。

        返回:
            编译后的 LangGraph（invoke 入参/出参均为 _AgentState 口径的 dict）。
        注意:
            子图有意不挂 checkpointer：内层往返状态无需跨进程恢复，
            挂上会把每轮消息写进生成线程的 checkpoint，干扰 HITL 恢复语义。
        """
        builder = StateGraph(_AgentState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("tools", self._tools_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", self._route, {"tools": "tools", "end": END})
        # 工具执行后：命中预算/上限（stop_reason 已置位）则直接收敛，不再进入下一轮模型调用
        builder.add_conditional_edges("tools", self._route_after_tools, {"agent": "agent", "end": END})
        return builder.compile()

    def _route(self, state: _AgentState) -> str:
        """条件边：本轮该继续执行工具，还是收敛结束。

        参数:
            state: 子图状态（读 stop_reason / turns / 最后一条 assistant 消息）。
        返回:
            "tools"（继续执行工具）或 "end"（收敛到 END）。
        注意:
            turns >= max_turns 是**结构性硬护栏**（有界性保证，即使调用方传入了不含
            限制策略的中间件链也不会无限循环）；更紧的成本策略由
            ModelCallLimitMiddleware 提供（它可在更小的阈值上取消下一轮模型调用）。
        """
        if state.get("stop_reason"):
            return "end"
        if int(state.get("turns") or 0) >= self._budget.max_turns:
            return "end"
        return "tools" if self._last_tool_calls(state) else "end"

    def _route_after_tools(self, state: _AgentState) -> str:
        """工具执行后的条件边：是否回到下一轮模型调用。

        参数:
            state: 子图状态（读 stop_reason）。
        返回:
            "agent"（继续下一轮模型调用）或 "end"（已命中预算/上限，立即收敛）。
        注意:
            工具上限（ToolCallLimitMiddleware）是在**工具节点内部**置位 stop_reason 的，
            若此处不收敛，就会白跑一轮模型调用（既费 token 又让终止原因被覆盖）。
        """
        return "end" if state.get("stop_reason") else "agent"

    def _last_tool_calls(self, state: _AgentState) -> list[dict]:
        """取出最后一条 assistant 消息里的 tool_calls（统一口径）。

        参数:
            state: 子图状态（读 messages）。
        返回:
            normalize_tool_calls() 口径的调用列表；无 assistant 消息或无 tool_calls 时返回 []。
        """
        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, dict) and message.get("role") == "assistant":
                return normalize_tool_calls(message.get("tool_calls"))
        return []

    def _agent_node(self, state: _AgentState) -> dict:
        """模型轮次节点：中间件 before_model → 运行时调用 → after_model → 追加 assistant 消息。

        参数:
            state: 子图状态（读 messages / turns）。
        返回:
            增量 dict：turns（本轮轮次）、messages（assistant 消息，含可选的 tool_calls）、
            trace（本轮记录）、context（助手文本，若有）、stop_reason（模型收敛或运行时错误时置位）。
        """
        turn = int(state.get("turns") or 0) + 1
        self._budget.turns_used = turn
        ctx = AgentContext(
            thread_id=self._thread_id,
            turn=turn,
            budget=self._budget,
            messages=list(state.get("messages") or []),
            tool_names=self.registry.names(),
        )
        for middleware in self.middlewares:
            middleware.before_model(ctx)
        if ctx.stop_reason:
            return {
                "turns": turn,
                "stop_reason": ctx.stop_reason,
                "trace": [{"turn": turn, "event": "model_skipped", "reason": ctx.stop_reason}],
            }
        self._model_calls += 1
        try:
            response = self.runtime.run_turn(
                list(state.get("messages") or []),
                tools=self.registry.to_openai_tools(),
            )
        except Exception as exc:  # noqa: BLE001 研究步骤失败不应打断生成主链路
            self._error = f"{type(exc).__name__}: {exc}"
            return {
                "turns": turn,
                "stop_reason": "runtime_error",
                "trace": [{"turn": turn, "event": "runtime_error", "error": self._error}],
            }
        for middleware in self.middlewares:
            response = middleware.after_model(ctx, response) or response
        calls = normalize_tool_calls((response or {}).get("tool_calls"))
        content = str((response or {}).get("content") or "")
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False, default=str),
                    },
                }
                for call in calls
            ]
        update: dict[str, Any] = {
            "turns": turn,
            "messages": [assistant],
            "trace": [
                {"turn": turn, "event": "model", "content": content[:200], "tool_calls": [c["name"] for c in calls]}
            ],
        }
        if content:
            update["context"] = content
        if not calls:
            update["stop_reason"] = "completed"
        return update

    def _tools_node(self, state: _AgentState) -> dict:
        """工具执行节点：对最后一条 assistant 消息里的 tool_calls 逐个执行。

        参数:
            state: 子图状态（读 messages / turns）。
        返回:
            增量 dict：messages（tool 角色结果消息，逐条回填 call_id）、
            trace（调用记录）、tool_calls_used（当前用量）、
            stop_reason（工具上限触发时置位以立即收敛）。
        注意:
            · 单个工具失败不中断其余调用：模型可在下一轮看到失败结果并换策略；
            · 事件 agent.tool 逐条发布，供前端 SSE 展示「正在查什么」。
        """
        calls = self._last_tool_calls(state)
        messages: list[dict] = []
        trace: list[dict] = []
        stop_reason = ""
        for raw in calls:
            call = ToolCall(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                arguments=dict(raw.get("arguments") or {}),
            )
            ctx = AgentContext(
                thread_id=self._thread_id,
                turn=int(state.get("turns") or 0),
                budget=self._budget,
                tool_names=self.registry.names(),
            )
            result = self._dispatch(call, ctx)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.to_message_content()}
            )
            entry = {"turn": ctx.turn, "event": "tool", "tool": call.name, "ok": result.ok}
            if result.error:
                entry["error"] = result.error
            trace.append(entry)
            self._publish(
                {"type": "agent.tool", "data": {"thread_id": self._thread_id, "tool": call.name, "ok": result.ok}}
            )
            if ctx.stop_reason and not stop_reason:
                stop_reason = ctx.stop_reason
        update: dict[str, Any] = {
            "messages": messages,
            "trace": trace,
            "tool_calls_used": self._budget.tool_calls_used,
        }
        if stop_reason:
            update["stop_reason"] = stop_reason
        return update

    def _dispatch(self, call: ToolCall, ctx: AgentContext) -> ToolResult:
        """按中间件洋葱顺序执行一次工具调用（最内层是 ToolRegistry.run）。

        参数:
            call: 本次工具调用。
            ctx: 共享上下文（传给各中间件钩子）。
        返回:
            ToolResult（正常结果、被拦截结果或兜底失败结果）。
        """
        def _base(inner: ToolCall) -> ToolResult:
            """最内层执行体：直接落到工具注册表。

            参数:
                inner: 本次工具调用。
            返回:
                ToolResult（未知工具由注册表收敛为失败结果）。
            """
            return self.registry.run(inner)

        handler: Callable[[ToolCall], ToolResult] = _base
        for middleware in reversed(self.middlewares):
            handler = self._bind_wrap(middleware, ctx, handler)
        return handler(call)

    def _bind_wrap(
        self,
        middleware: AgentMiddleware,
        ctx: AgentContext,
        next_handler: Callable[[ToolCall], ToolResult],
    ) -> Callable[[ToolCall], ToolResult]:
        """把某个中间件的 wrap_tool_call 包在 next_handler 外层。

        参数:
            middleware: 中间件实例。
            ctx: 共享上下文（闭包绑定，保证钩子拿到同一份预算）。
            next_handler: 被包裹的内层执行体。
        返回:
            包装后的执行体（签名与 ToolCall 一致）。
        """

        def _wrapped(call: ToolCall) -> ToolResult:
            """中间件包装后的执行体。

            参数:
                call: 本次工具调用。
            返回:
                中间件处理后的 ToolResult。
            """
            return middleware.wrap_tool_call(call, ctx=ctx, handler=next_handler)

        return _wrapped

    def _publish(self, payload: dict) -> None:
        """向 `evt:{thread_id}` 发布 Agent 过程事件。

        参数:
            payload: 事件载荷（JSON 可序列化 dict）。
        返回:
            无返回值；未注入 EventBus 时直接返回（本地/单测场景无需事件）。
        异常:
            Exception: 发布失败按 ports/event_bus.py 契约上抛，不静默吞。
        """
        if self.event_bus is None:
            return
        self.event_bus.publish(f"evt:{self._thread_id}", payload)
