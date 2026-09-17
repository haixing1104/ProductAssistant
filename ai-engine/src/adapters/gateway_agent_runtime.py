"""GatewayAgentRuntime：把 LLMGateway（chat_with_tools）适配为 AgentRuntime 端口。

为什么需要这一层:
    · AgentLoop 依赖 ports/agent_runtime.py 的 `run_turn(messages, tools=...)` 契约；
    · 真实实现落在 LLMGateway.chat_with_tools（智谱 OpenAI 兼容 tools 协议），
      两者签名不同（一个是端口语义、一个是协议方法），用一个薄适配器对齐，
      既不改动 LLMGateway 既有方法，也不让内核直接依赖具体供应商实现；
    · 将来换 LangChain 官方 create_agent，只需新增另一个实现 AgentRuntime 的 adapter。

环境变量（工厂）:
    LLM_PROVIDER / ZHIPU_API_KEY 等沿用 adapters/llm_zhipu.build_zhipu_gateway_from_env()
    的约定：未配置 Key 时工厂返回 None（Agent 节点随之 no-op，不产生任何额外成本）。
"""

from __future__ import annotations

from ..ports import AgentRuntime, LLMGateway, normalize_tool_calls

__all__ = ["GatewayAgentRuntime", "build_agent_runtime_from_env"]


class GatewayAgentRuntime(AgentRuntime):
    """AgentRuntime 的网关实现：一次 run_turn = 一次 LLMGateway.chat_with_tools。"""

    def __init__(self, gateway: LLMGateway) -> None:
        """初始化。

        参数:
            gateway: 已配置好的 LLM 网关（须支持 chat_with_tools；不支持时会在首个
                run_turn 抛 NotImplementedError，被 AgentLoop 收敛为 runtime_error）。
        """
        self._gateway = gateway

    def run_turn(self, messages: list[dict], *, tools: list[dict] | None = None,
                 tool_choice: str = "auto") -> dict:
        """执行一次模型轮次（带工具声明）。

        参数:
            messages: OpenAI 兼容消息列表（含历史 assistant/tool 消息）。
            tools: tools 声明数组；None/空表示不带工具。
            tool_choice: 工具选择策略（默认 "auto"）。
        返回:
            {"content": str, "tool_calls": [{"id","name","arguments"}], "model": str | None}。
        异常:
            Exception: 网关异常（网络/HTTP/协议）原样上抛，由 AgentLoop 收敛为 runtime_error。
        """
        response = self._gateway.chat_with_tools(
            messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        return {
            "content": str((response or {}).get("content") or ""),
            "tool_calls": normalize_tool_calls((response or {}).get("tool_calls")),
            "model": (response or {}).get("model"),
        }


def build_agent_runtime_from_env() -> AgentRuntime | None:
    """按环境变量装配 Agent 运行时（无 Key / provider=mock 时返回 None）。

    返回:
        GatewayAgentRuntime 实例；未配置可用 LLM 网关时返回 None
        （调用方应把 None 传给 build_workflow，agent_research 节点将 no-op）。
    注意:
        复用 build_zhipu_gateway_from_env 的判定逻辑（LLM_PROVIDER=mock 或缺少
        ZHIPU_API_KEY 即视为未配置），保证「生成用哪套模型，Agent 就用哪套」。
    """
    from .llm_zhipu import build_zhipu_gateway_from_env

    gateway = build_zhipu_gateway_from_env()
    if gateway is None:
        return None
    return GatewayAgentRuntime(gateway)
