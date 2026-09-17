"""Agent 研究节点：用只读工具取证 → 结论写入 agent_context（供 generate 使用）。

职责边界:
    · 与其它 node 一致：只读 ListingState、返回增量 dict；不直连 DB/Redis/HTTP，
      取数一律经 ports（BusinessReader / RuleEngine / RAGStore）注入；
    · 未注入 runtime（未配置带 tools 的模型）时**直接 no-op**（返回 {}），
      因此现有链路、mock 降级路径与全部既有测试的行为保持不变。

产出（ListingState 增量的三个字段）:
    · agent_context   —— 研究结论文本，由 node_generate 拼进生成提示词；
    · agent_trace     —— 每轮模型/工具调用的审计轨迹（JSON 安全）；
    · tool_calls_used —— 本次研究实际消耗的工具调用次数。

事件:
    过程事件写 `evt:{thread_id}`（stage.researching / agent.tool / agent.done），
    供 SSE 展示「AI 正在核对哪些数据」；事件发布失败按端口契约上抛（不静默吞）。
"""

from __future__ import annotations

from typing import Any

from ...ports import AgentRuntime, BusinessReader, EventBus, RAGStore, RuleEngine
from ..agent import DEFAULT_MAX_TOOL_CALLS, DEFAULT_MAX_TURNS, AgentLoop, build_registry
from ..agent.middleware import AgentMiddleware
from ..state import ListingState, ensure_state

__all__ = ["agent_research_node"]


def _build_question(state: ListingState) -> str:
    """依据当前商品素材拼装研究问题（模型据此决定调哪些工具）。

    参数:
        state: 当前图状态；读 raw_product_info（标题/价格/库存）。
    返回:
        交给 Agent 的问题文本（含事实基线初值，要求模型用工具核对后再给要点）。
    """
    info: dict[str, Any] = state.raw_product_info or {}
    title = str(info.get("title") or "商品")
    price = info.get("base_price")
    stock = info.get("stock_status")
    return (
        f"商品「{title}」即将撰写上架文案。事实基线初值：base_price={price}，stock_status={stock}。\n"
        "请先用可用工具核对：①本商品真实素材；②历史文案版本；③历史评估违规点；④历史审批驳回意见。\n"
        "然后输出必须遵守的事实要点（不得与工具返回数据冲突），并指出需要规避的历史违规点。"
    )


def agent_research_node(
    state: ListingState,
    *,
    agent_runtime: AgentRuntime | None = None,
    reader: BusinessReader | None = None,
    rule_engine: RuleEngine | None = None,
    rag_store: RAGStore | None = None,
    middlewares: list[AgentMiddleware] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    event_bus: EventBus | None = None,
) -> dict:
    """执行一次只读研究，把结论与轨迹写回状态增量。

    参数:
        state: 当前图状态；读 thread_id / product_id / org_id / raw_product_info。
        agent_runtime: Agent 运行时（带 tools 的模型）；None 时本节点直接短路（no-op）。
            ⚠ 参数名**不能**叫 `runtime`：那是 LangGraph 的保留注入名，
            框架会无视 partial 绑定强行注入它自己的 Runtime 对象
            （实测：`partial(fn, runtime=None)` 依然收到 `Runtime(context=None, ...)`），
            会让「未注入就 no-op」失效、真注入时也拿不到自己的运行时。
            回归断言见 tests/test_agent_tools.py::test_node_avoids_langgraph_reserved_runtime_param。
        reader: 业务只读端口；None 时不注册数据库取数工具（工具表可能为空 → 同样短路）。
        rule_engine: 合规规则引擎（job 载荷规则快照）；None 时不注册 scan_compliance。
        rag_store: RAG 向量库端口；None 时不注册 retrieve_similar_copy。
        middlewares: 中间件链；None 时用 AgentLoop 的默认链（预算熔断 + 工具上限 + 重试兜底）。
        max_turns: 模型调用上限（预算熔断阈值）。
        max_tool_calls: 工具调用上限。
        event_bus: 事件端口；None 时不发过程事件。
    返回:
        增量 dict：
          · 运行时/工具表为空 → {}（不产生任何状态变更，行为与未接入 Agent 时一致）；
          · 正常 → {"agent_context": ..., "agent_trace": [...], "tool_calls_used": n}。
    注意:
        研究失败（LLM 异常 / 预算到顶）不抛异常：AgentLoop 内部收敛为
        stop_reason 并返回已获得的结论与轨迹，保证生成主链路可继续。
    """
    if agent_runtime is None:
        return {}
    state = ensure_state(state)
    registry = build_registry(
        org_id=state.org_id,
        product_id=state.product_id,
        reader=reader,
        rule_engine=rule_engine,
        rag_store=rag_store,
    )
    if len(registry) == 0:
        return {}
    if event_bus is not None:
        event_bus.publish(
            f"evt:{state.thread_id}",
            {
                "type": "stage.researching",
                "data": {
                    "product_id": state.product_id,
                    "tools": registry.names(),
                    "max_turns": max_turns,
                    "max_tool_calls": max_tool_calls,
                },
            },
        )
    result = AgentLoop(
        runtime=agent_runtime,
        registry=registry,
        middlewares=middlewares,
        max_turns=max_turns,
        max_tool_calls=max_tool_calls,
        event_bus=event_bus,
    ).run(thread_id=state.thread_id, question=_build_question(state))
    return {
        "agent_context": result.context,
        "agent_trace": result.trace,
        "tool_calls_used": result.tool_calls_used,
    }
